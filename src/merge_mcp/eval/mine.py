"""Mine benchmark candidates from a repo's history.

A candidate is a commit that (a) references a closed issue, (b) changes at least one test
file and a small number of Python source files, and (c) touches nothing else that affects
behavior (no dependency or config changes). The commit's parent becomes `base_commit`;
its diff is split into `test_patch` and `gold_patch`.

Diffs come from git, so mining costs almost no API quota. Candidates are NOT trusted
until `validate` has executed them.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path, PurePosixPath

from ..github import GitHubClient, GitHubError, RateLimited
from ..workspace import git
from .tasks import Task, is_test_path

_DOC_SUFFIXES = {".md", ".rst", ".txt"}
_DOC_DIRS = {"docs", "doc", "changelog", "changes", "news", "newsfragments", "changelog.d"}


def issue_refs(message: str, repo: str) -> list[int]:
    """Issue numbers a commit message points at, closing-keyword references first."""
    target = rf"(?:#|https://github\.com/{re.escape(repo)}/issues/)(\d+)"
    closing = re.findall(
        rf"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+{target}", message, re.IGNORECASE
    )
    other = re.findall(target, message)
    ordered: list[int] = []
    for n in [*closing, *other]:
        if int(n) not in ordered:
            ordered.append(int(n))
    return ordered


def classify(
    changes: list[tuple[str, str]], max_src_files: int
) -> tuple[list[str], list[str]] | None:
    """Return (test files, source files) if the commit is a usable shape, else None."""
    tests, src = [], []
    for status, path in changes:
        p = PurePosixPath(path)
        if status not in ("A", "M"):
            return None
        if p.suffix == ".py":
            (tests if is_test_path(path) else src).append(path)
        elif p.suffix in _DOC_SUFFIXES or (_DOC_DIRS & set(p.parts[:-1])):
            continue  # changelog and docs edits are ignored, not graded
        else:
            return None
    if not tests or not src or len(src) > max_src_files:
        return None
    return tests, src


def changed_lines(patch: str) -> int:
    return sum(
        1
        for ln in patch.splitlines()
        if (ln.startswith("+") and not ln.startswith("+++"))
        or (ln.startswith("-") and not ln.startswith("---"))
    )


class IssueLookup:
    """Fetch issue text, falling back to one bulk search if per-issue calls are refused."""

    def __init__(self, gh: GitHubClient, repo: str):
        self.gh, self.repo = gh, repo
        self._bulk: dict[int, dict] | None = None

    async def _load_bulk(self) -> dict[int, dict]:
        found: dict[int, dict] = {}
        for page in (1, 2, 3):
            data = await self.gh.get(
                "/search/issues", q=f"repo:{self.repo} is:issue is:closed", per_page=100, page=page
            )
            for item in data.get("items", []):
                found[item["number"]] = item
            if len(data.get("items", [])) < 100:
                break
        return found

    async def get(self, number: int) -> dict | None:
        if self._bulk is None:
            try:
                issue = await self.gh.issue(self.repo, number)
                return None if "pull_request" in issue else issue
            except RateLimited:
                raise
            except GitHubError as exc:
                if "Not found" in str(exc):
                    return None
                self._bulk = await self._load_bulk()
        item = self._bulk.get(number)
        return None if item is None or "pull_request" in item else item


async def mine_repo(
    gh: GitHubClient,
    repo: str,
    *,
    limit: int = 10,
    max_commits: int = 400,
    max_src_files: int = 3,
    max_changed_lines: int = 150,
    clone_url: str | None = None,
) -> list[Task]:
    lookup = IssueLookup(gh, repo)
    tasks: list[Task] = []
    with tempfile.TemporaryDirectory(prefix="merge-mcp-mine-") as tmp:
        clone = Path(tmp) / "repo"
        await git(
            "clone",
            "-q",
            "--no-tags",
            f"--depth={max_commits + 1}",
            clone_url or f"https://github.com/{repo}.git",
            str(clone),
            cwd=Path(tmp),
        )
        log = await git(
            "log",
            "--no-merges",
            "-n",
            str(max_commits),
            "--format=%H%x1f%P%x1f%s%x1f%b%x1e",
            cwd=clone,
        )
        for record in log.split("\x1e"):
            if len(tasks) >= limit:
                break
            fields = record.strip("\n").split("\x1f")
            if len(fields) != 4 or not fields[1]:
                continue
            sha, parent, subject, body = fields[0], fields[1].split()[0], fields[2], fields[3]
            numbers = issue_refs(f"{subject}\n{body}", repo)
            if not numbers:
                continue
            status = await git("show", "--name-status", "--no-renames", "--format=", sha, cwd=clone)
            changes = [tuple(row.split("\t", 1)) for row in status.splitlines() if "\t" in row]
            shape = classify(changes, max_src_files)  # type: ignore[arg-type]
            if shape is None:
                continue
            tests, src = shape
            gold = await git("diff", "--no-color", parent, sha, "--", *src, cwd=clone, check=False)
            if not gold or changed_lines(gold) > max_changed_lines:
                continue
            test_patch = await git("diff", "--no-color", parent, sha, "--", *tests, cwd=clone)

            issue = None
            for number in numbers[:3]:
                issue = await lookup.get(number)
                if issue:
                    break
            if not issue or len((issue.get("body") or "").strip()) < 40:
                continue
            tasks.append(
                Task(
                    task_id=f"{repo.replace('/', '__')}-{issue['number']}",
                    repo=repo,
                    base_commit=parent,
                    issue_number=issue["number"],
                    problem_statement=f"{issue['title']}\n\n{(issue.get('body') or '').strip()}",
                    gold_patch=gold,
                    test_patch=test_patch,
                    test_files=sorted(tests),
                    source_commit=sha,
                )
            )
    return tasks
