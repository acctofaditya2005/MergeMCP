"""Workspaces: an isolated checkout of one repo for one contribution.

A workspace is identified by a server-minted handle (`ws_...`) that the client passes to
every later call. All state lives on disk under `<home>/workspaces/<id>/`, so the server
keeps nothing per connection and a restart loses nothing:

    <id>/meta.json       the Workspace record
    <id>/playbook.json   cached conventions (see conventions.py)
    <id>/repo/           the git checkout
    <id>/PR_DRAFT.md     written by the handoff step

File access is jailed to `repo/`. There is deliberately no commit, push or fork here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings
from .github import parse_repo
from .models import (
    DiffResult,
    FileContent,
    FileList,
    SearchMatch,
    SearchResult,
    Workspace,
    WriteResult,
)
from .sandbox import STATE_DIR, VENV_DIR

MAX_READ_BYTES = 400_000
MAX_WRITE_BYTES = 1_000_000
MAX_DIFF_CHARS = 60_000
_PROTECTED = (".git", VENV_DIR, STATE_DIR)
_ID = re.compile(r"^ws_[0-9a-f]{10}$")


class WorkspaceError(Exception):
    """A user-facing workspace problem (bad path, unknown id, git failure)."""


async def git(*args: str, cwd: Path, check: bool = True, stdin: str | None = None) -> str:
    """Run git with hooks disabled and prompts off. Returns stdout."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "advice.detachedHead=false",
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
    if check and proc.returncode != 0:
        raise WorkspaceError(
            f"git {args[0]} failed: {err.decode('utf-8', 'replace').strip()[-500:]}"
        )
    return out.decode("utf-8", "replace")


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "change"


def branch_name(issue_number: int | None, title: str | None) -> str:
    if issue_number is not None:
        return f"fix/issue-{issue_number}" + (f"-{slugify(title, 30)}" if title else "")
    return f"contrib/{slugify(title or 'change')}"


class WorkspaceManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ---- lifecycle ----------------------------------------------------------------

    def _resolve_source(self, source: str) -> tuple[str, str]:
        """Return (display name, clone URL). Only GitHub HTTPS unless local is allowed."""
        if source.startswith(("/", "file://")):
            if not self.settings.allow_local_sources:
                raise WorkspaceError("Local repositories are disabled on this server.")
            path = Path(source.removeprefix("file://"))
            return f"local/{path.name}", str(path)
        repo = parse_repo(source)
        return repo, f"https://github.com/{repo}.git"

    async def create(
        self,
        source: str,
        *,
        ref: str | None = None,
        issue_number: int | None = None,
        issue_title: str | None = None,
    ) -> Workspace:
        repo, url = self._resolve_source(source)
        if ref is not None and not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", ref):
            raise WorkspaceError(f"Invalid git ref: {ref!r}")
        ws_id = f"ws_{secrets.token_hex(5)}"
        root = self.settings.workspaces_dir / ws_id
        checkout = root / "repo"
        checkout.mkdir(parents=True)
        try:
            await git("init", "-q", cwd=checkout)
            await git("remote", "add", "origin", url, cwd=checkout)
            await git(
                "fetch",
                "-q",
                "--no-tags",
                "--no-recurse-submodules",
                f"--depth={self.settings.clone_depth}",
                "origin",
                ref or "HEAD",
                cwd=checkout,
            )
            branch = branch_name(issue_number, issue_title)
            await git("checkout", "-q", "-B", branch, "FETCH_HEAD", cwd=checkout)
            base = (await git("rev-parse", "HEAD", cwd=checkout)).strip()
            self._exclude(checkout, [f"{VENV_DIR}/", f"{STATE_DIR}/"])
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        ws = Workspace(
            workspace_id=ws_id,
            repo=repo,
            source_url=url,
            base_commit=base,
            branch=branch,
            issue_number=issue_number,
            path=str(checkout),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        (root / "meta.json").write_text(ws.model_dump_json(indent=2))
        return ws

    @staticmethod
    def _exclude(checkout: Path, patterns: list[str]) -> None:
        """Keep server-created paths (virtualenvs, caches) out of `git status` and diffs."""
        path = checkout / ".git" / "info" / "exclude"
        path.parent.mkdir(exist_ok=True)
        existing = path.read_text().splitlines() if path.exists() else []
        missing = [p for p in patterns if p not in existing]
        if missing:
            with path.open("a") as fh:
                fh.write("\n" + "\n".join(missing) + "\n")

    async def untracked(self, workspace_id: str) -> set[str]:
        out = await git(
            "ls-files",
            "--others",
            "--exclude-standard",
            "--directory",
            cwd=self.checkout(workspace_id),
        )
        return {line for line in out.splitlines() if line}

    def exclude(self, workspace_id: str, patterns: list[str]) -> None:
        self._exclude(self.checkout(workspace_id), patterns)

    def update(self, workspace_id: str, **changes: object) -> Workspace:
        ws = self.get(workspace_id).model_copy(update=changes)
        (self.root(workspace_id) / "meta.json").write_text(ws.model_dump_json(indent=2))
        return ws

    def root(self, workspace_id: str) -> Path:
        if not _ID.match(workspace_id):
            raise WorkspaceError(f"Malformed workspace id: {workspace_id!r}")
        root = self.settings.workspaces_dir / workspace_id
        if not (root / "meta.json").exists():
            raise WorkspaceError(
                f"Unknown workspace {workspace_id!r}. Create one with start_workspace."
            )
        return root

    def get(self, workspace_id: str) -> Workspace:
        return Workspace.model_validate_json((self.root(workspace_id) / "meta.json").read_text())

    def checkout(self, workspace_id: str) -> Path:
        return self.root(workspace_id) / "repo"

    def list(self) -> list[Workspace]:
        base = self.settings.workspaces_dir
        if not base.exists():
            return []
        metas = sorted(base.glob("ws_*/meta.json"))
        return [Workspace.model_validate_json(m.read_text()) for m in metas]

    def delete(self, workspace_id: str) -> None:
        """Remove a workspace. Toolchain caches can hold read-only files, so make each
        directory tree writable first instead of relying on an error callback (whose
        name differs between Python versions)."""
        root = self.root(workspace_id)
        for directory, _subdirs, _files in os.walk(root):
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        shutil.rmtree(root)

    def read_json(self, workspace_id: str, name: str) -> dict | None:
        path = self.root(workspace_id) / name
        return json.loads(path.read_text()) if path.exists() else None

    def write_json(self, workspace_id: str, name: str, data: dict) -> None:
        (self.root(workspace_id) / name).write_text(json.dumps(data, indent=2))

    # ---- jailed file access -------------------------------------------------------

    def resolve(self, workspace_id: str, rel: str, *, for_write: bool = False) -> Path:
        """Map a repo-relative path to a real path, refusing anything outside the checkout."""
        root = self.checkout(workspace_id).resolve()
        if not rel or rel.startswith(("/", "~")) or "\x00" in rel:
            raise WorkspaceError(f"Path must be relative to the repository root: {rel!r}")
        target = (root / rel).resolve()
        if target != root and root not in target.parents:
            raise WorkspaceError(f"Path escapes the workspace: {rel!r}")
        parts = target.relative_to(root).parts
        if for_write and parts and parts[0] in _PROTECTED:
            raise WorkspaceError(f"Writing under {parts[0]}/ is not allowed.")
        return target

    async def list_files(self, workspace_id: str, prefix: str = "", limit: int = 500) -> FileList:
        out = await git(
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            cwd=self.checkout(workspace_id),
        )
        prefix = prefix.strip("/")
        files = sorted(
            f for f in out.splitlines() if not prefix or f == prefix or f.startswith(prefix + "/")
        )
        return FileList(files=files[:limit], truncated=len(files) > limit)

    def read_file(
        self, workspace_id: str, path: str, start_line: int = 1, max_lines: int = 400
    ) -> FileContent:
        target = self.resolve(workspace_id, path)
        if not target.is_file():
            raise WorkspaceError(f"No such file: {path}")
        if target.stat().st_size > MAX_READ_BYTES:
            raise WorkspaceError(f"{path} is larger than {MAX_READ_BYTES} bytes; use search_code.")
        try:
            lines = target.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise WorkspaceError(f"{path} is not UTF-8 text.") from exc
        start = max(1, start_line)
        chunk = lines[start - 1 : start - 1 + max(1, max_lines)]
        return FileContent(
            path=path,
            start_line=start,
            end_line=start + len(chunk) - 1 if chunk else start - 1,
            total_lines=len(lines),
            content="\n".join(chunk),
        )

    def write_file(self, workspace_id: str, path: str, content: str) -> WriteResult:
        if len(content.encode()) > MAX_WRITE_BYTES:
            raise WorkspaceError("Refusing to write more than 1 MB in one call.")
        target = self.resolve(workspace_id, path, for_write=True)
        if target.is_dir():
            raise WorkspaceError(f"{path} is a directory.")
        created = not target.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return WriteResult(path=path, bytes_written=len(content.encode()), created=created)

    def edit_file(self, workspace_id: str, path: str, old_text: str, new_text: str) -> WriteResult:
        """Replace exactly one occurrence. Ambiguous or missing matches are errors."""
        target = self.resolve(workspace_id, path, for_write=True)
        if not target.is_file():
            raise WorkspaceError(f"No such file: {path}")
        text = target.read_text(encoding="utf-8")
        count = text.count(old_text) if old_text else 0
        if count != 1:
            raise WorkspaceError(
                f"old_text must match exactly once in {path}; it matched {count} times."
            )
        return self.write_file(workspace_id, path, text.replace(old_text, new_text, 1))

    async def search(
        self, workspace_id: str, pattern: str, path_prefix: str = "", limit: int = 80
    ) -> SearchResult:
        args = ["grep", "-n", "-I", "--untracked", "-E", "-e", pattern]
        if path_prefix:
            self.resolve(workspace_id, path_prefix)
            args += ["--", path_prefix]
        checkout = self.checkout(workspace_id)
        proc_out = await git(*args, cwd=checkout, check=False)
        matches = []
        for line in proc_out.splitlines():
            parts = line.split(":", 2)
            if len(parts) == 3 and parts[1].isdigit():
                matches.append(SearchMatch(path=parts[0], line=int(parts[1]), text=parts[2][:300]))
        return SearchResult(matches=matches[:limit], truncated=len(matches) > limit)

    # ---- diff ---------------------------------------------------------------------

    async def diff(self, workspace_id: str, max_chars: int = MAX_DIFF_CHARS) -> DiffResult:
        """Everything changed since the base commit, including new untracked files."""
        ws = self.get(workspace_id)
        checkout = self.checkout(workspace_id)
        await git("add", "-A", "-N", cwd=checkout)  # intent-to-add so new files show up
        base = ws.base_commit
        full = await git("diff", "--no-color", "--no-ext-diff", base, cwd=checkout)
        names = (await git("diff", "--name-only", base, cwd=checkout)).splitlines()
        ins = dels = 0
        for row in (await git("diff", "--numstat", base, cwd=checkout)).splitlines():
            a, d, *_ = row.split("\t")
            ins += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        return DiffResult(
            base_commit=base,
            files_changed=names,
            insertions=ins,
            deletions=dels,
            diff=full[:max_chars],
            truncated=len(full) > max_chars,
        )
