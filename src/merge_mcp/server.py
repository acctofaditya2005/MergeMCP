"""The MCP server: tool definitions only.

Each tool is a thin adapter: validate input, call a service module, return a typed model.
The logic lives in the service modules, which are plain Python and are tested without MCP
in the loop.

State model: the server is stateless per connection. Cross-call state travels as
server-minted handles (`workspace_id`, `job_id`) that the client passes back as ordinary
tool arguments.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import __version__, checks, deps, discovery, pr_status, precedents, readiness
from .checks import CheckError
from .config import Settings
from .conventions import build_playbook, resolve_tooling
from .github import GitHubClient, GitHubError, parse_issue_url, parse_repo
from .handoff import build_draft
from .jobs import JobRegistry, UnknownJob
from .models import (
    CheckKind,
    CheckResult,
    DependencyIssueResult,
    DiffResult,
    FileContent,
    FileList,
    IssueAssessment,
    IssueSearchResult,
    JobHandle,
    JobStatus,
    Playbook,
    PrecedentList,
    PullRequestDraft,
    PullRequestStatus,
    Readiness,
    SearchResult,
    Workspace,
    WriteResult,
)
from .rules import Rules, load_rules
from .sandbox import Runner, SandboxUnavailable, make_runner
from .workspace import WorkspaceError, WorkspaceManager

INSTRUCTIONS = """\
merge-mcp helps a person contribute to an open-source GitHub project in any language.

Typical flow:
1. Pick an issue that is actually available: find_issues_in_dependencies (issues in
   libraries the user already uses) or find_issues, then assess_issue on the one you choose.
2. start_workspace, then get_job until it succeeds: clones the repo and returns a playbook
   (how to build and test it, its conventions, and what past merged changes contained).
3. find_precedents on the files you expect to touch, to see how similar changes were made.
4. Read and edit code with list_files / search_code / read_file / edit_file / write_file.
5. start_check(kind="setup") once, then kind="test" and kind="lint"; poll get_job.
6. check_readiness: fix everything it flags that you can.
7. prepare_pull_request, show the user the checklist and commands, and stop.
8. Later, check_pull_request on the opened pull request to see what it needs to get merged.

This server never commits, pushes, forks or opens pull requests. prepare_pull_request
returns a draft and the commands for the human to run after reviewing the diff. Do not
try to submit on the user's behalf by other means.
"""

READ_ONLY = {"readOnlyHint": True, "idempotentHint": True}
READ_ONLY_REMOTE = {**READ_ONLY, "openWorldHint": True}
WRITES_WORKSPACE = {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False}

WorkspaceId = Annotated[str, Field(description="Handle returned by start_workspace (ws_...).")]
RepoPath = Annotated[str, Field(description="Path relative to the repository root.")]

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])
_USER_ERRORS = (WorkspaceError, GitHubError, CheckError, SandboxUnavailable, UnknownJob, ValueError)


def user_errors(fn: F) -> F:
    """Report expected failures to the model as tool errors it can read and act on."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except _USER_ERRORS as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


@dataclass
class Services:
    """Everything a tool needs. Built once; tests and the benchmark build their own."""

    settings: Settings
    rules: Rules
    github: GitHubClient
    registries: httpx.AsyncClient
    workspaces: WorkspaceManager
    jobs: JobRegistry
    runner: Runner

    def playbook(self, workspace_id: str) -> Playbook:
        data = self.workspaces.read_json(workspace_id, "playbook.json")
        if data is None:
            raise WorkspaceError(f"Workspace {workspace_id} has no playbook yet.")
        return Playbook.model_validate(data)

    async def build_playbook(self, ws: Workspace, **options: Any) -> Playbook:
        """Build, cache and return the playbook for a workspace."""
        playbook = await build_playbook(
            self.workspaces.checkout(ws.workspace_id),
            self.rules,
            repo=ws.repo,
            base_commit=ws.base_commit,
            **options,
        )
        eco = self.rules.ecosystems.get(playbook.tooling.ecosystem or "")
        if eco and eco.exclude:
            self.workspaces.exclude(ws.workspace_id, eco.exclude)
        self.workspaces.write_json(
            ws.workspace_id, "playbook.json", playbook.model_dump(mode="json")
        )
        return playbook

    async def run_check(
        self, workspace_id: str, kind: CheckKind, test_ids: list[str] | None = None
    ) -> CheckResult:
        """Run setup, tests or the linter for a workspace and record the outcome."""
        timeout = (
            self.settings.setup_timeout_s if kind == "setup" else self.settings.check_timeout_s
        )
        before = await self.workspaces.untracked(workspace_id) if kind == "setup" else set()
        result = await checks.run_check(
            self.runner,
            self.workspaces.checkout(workspace_id),
            self.playbook(workspace_id),
            kind,
            test_ids=test_ids,
            timeout_s=timeout,
        )
        playbook = self.playbook(workspace_id)
        if kind == "setup" and not result.ok and playbook.tooling.source.startswith("ci:"):
            # CI steps do not always reproduce outside CI. Fall back once to the ecosystem's
            # own defaults, and say so in the playbook so it stays truthful.
            checkout = self.workspaces.checkout(workspace_id)
            defaults = resolve_tooling(checkout, self.rules, use_ci=False)
            if defaults.test_command:
                note = (
                    f"Setup with the commands from {playbook.tooling.source} failed; using "
                    f"{defaults.source} instead."
                )
                playbook = playbook.model_copy(
                    update={"tooling": defaults, "notes": [*playbook.notes, note]}
                )
                self.workspaces.write_json(
                    workspace_id, "playbook.json", playbook.model_dump(mode="json")
                )
                result = await checks.run_check(
                    self.runner, checkout, playbook, kind, timeout_s=timeout
                )
        if kind == "setup":
            # Whatever installation created (lockfiles, build output, caches) is not part
            # of the contribution. Excluding it here works for any toolchain.
            created = await self.workspaces.untracked(workspace_id) - before
            self.workspaces.exclude(workspace_id, sorted("/" + path for path in created))
        else:
            diff = await self.workspaces.diff(workspace_id, max_chars=10**9)
            record = self.workspaces.read_json(workspace_id, "checks.json") or {}
            record[kind] = {
                "ok": result.ok,
                "partial": bool(test_ids),
                "fingerprint": checks.diff_fingerprint(diff.diff),
            }
            self.workspaces.write_json(workspace_id, "checks.json", record)
        return result

    async def readiness(self, workspace_id: str) -> tuple[Readiness, DiffResult]:
        diff = await self.workspaces.diff(workspace_id, max_chars=10**9)
        report = readiness.assess(
            diff.files_changed,
            self.playbook(workspace_id),
            self.rules,
            last_checks=self.workspaces.read_json(workspace_id, "checks.json") or {},
            fingerprint=checks.diff_fingerprint(diff.diff),
        )
        return report, diff

    async def aclose(self) -> None:
        await self.github.aclose()
        await self.registries.aclose()


def build_services(
    settings: Settings | None = None,
    *,
    github: GitHubClient | None = None,
    runner: Runner | None = None,
    rules: Rules | None = None,
    registries: httpx.AsyncClient | None = None,
) -> Services:
    settings = settings or Settings.from_env()
    return Services(
        settings=settings,
        rules=rules or load_rules(settings.home / "rules.toml", settings.rules_path),
        github=github or GitHubClient(settings),
        registries=registries
        or httpx.AsyncClient(
            timeout=20.0, follow_redirects=True, headers={"User-Agent": "merge-mcp"}
        ),
        workspaces=WorkspaceManager(settings),
        jobs=JobRegistry(settings.home / "jobs"),
        runner=runner or make_runner(settings),
    )


def create_server(services: Services | None = None) -> FastMCP:
    svc = services or build_services()
    mcp = FastMCP("merge-mcp", instructions=INSTRUCTIONS, version=__version__)

    # ------------------------------------------------------------------ discovery

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    @user_errors
    async def find_issues(
        language: Annotated[
            str | None, Field(description="Primary repo language, e.g. 'go'. Omit for any.")
        ] = None,
        labels: Annotated[
            list[str] | None,
            Field(
                description="Issue labels that must all be present. "
                "Defaults to ['good first issue']."
            ),
        ] = None,
        keywords: Annotated[
            str | None, Field(description="Free-text terms, e.g. a library or topic.")
        ] = None,
        repo: Annotated[str | None, Field(description="Limit to one repo, 'owner/name'.")] = None,
        min_stars: Annotated[
            int | None, Field(ge=0, description="Skip repos with fewer stars.")
        ] = 50,
        updated_within_days: Annotated[
            int | None, Field(ge=1, description="Only issues with activity this recently.")
        ] = 90,
        limit: Annotated[int, Field(ge=1, le=25, description="Maximum candidates.")] = 10,
    ) -> IssueSearchResult:
        """Search GitHub for open, unassigned issues with no linked pull request, ranked by
        a viability score that explains every deduction. Use assess_issue for a deeper check
        of one issue before starting work."""
        return await discovery.find_issues(
            svc.github,
            svc.rules.viability,
            language=language,
            labels=labels,
            keywords=keywords,
            repo=parse_repo(repo) if repo else None,
            min_stars=min_stars,
            updated_within_days=updated_within_days,
            limit=limit,
        )

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    @user_errors
    async def find_issues_in_dependencies(
        manifests: Annotated[
            list[dict[str, str]] | None,
            Field(
                description="Dependency files from the user's own projects, each as "
                "{'filename': 'package.json', 'content': '<file text>'}. Recognised names "
                "include requirements*.txt, pyproject.toml, package.json, go.mod, Cargo.toml."
            ),
        ] = None,
        packages: Annotated[
            list[dict[str, str]] | None,
            Field(
                description="Or name packages directly, each as "
                "{'ecosystem': 'python', 'name': 'httpx'}."
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=25, description="Maximum candidates.")] = 10,
    ) -> DependencyIssueResult:
        """Find available issues in the libraries the user already depends on. Read the
        user's dependency files and pass them in; the server maps each package to its GitHub
        repository and searches those repos for newcomer-friendly issues, ranked by
        viability. People succeed more often on libraries they already use."""
        pairs: list[tuple[str, str]] = []
        for manifest in manifests or []:
            pairs += deps.parse_manifest(
                manifest.get("filename", ""), manifest.get("content", ""), svc.rules
            )
        for package in packages or []:
            eco, name = package.get("ecosystem", ""), package.get("name", "")
            if eco not in svc.rules.ecosystems:
                known = ", ".join(svc.rules.ecosystems)
                raise ValueError(f"Unknown ecosystem {eco!r}. Known: {known}")
            pairs.append((eco, name))
        if not pairs:
            raise ValueError("Provide manifests or packages.")
        return await deps.find_issues_in_dependencies(
            svc.github, svc.registries, svc.rules, pairs, limit=limit
        )

    @mcp.tool(annotations=READ_ONLY_REMOTE)
    @user_errors
    async def assess_issue(
        issue_url: Annotated[str, Field(description="https://github.com/owner/name/issues/N")],
    ) -> IssueAssessment:
        """Check whether one issue is worth starting: is it open, unassigned, unclaimed, free
        of competing pull requests, and in a repo that merges outside contributions? Returns
        the issue text plus a 0-100 score with reasons."""
        repo, number = parse_issue_url(issue_url)
        return await discovery.assess_issue(svc.github, repo, number, svc.rules.viability)

    # ------------------------------------------------------------------ workspace

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    @user_errors
    async def start_workspace(
        repo: Annotated[str | None, Field(description="'owner/name' or a github.com URL.")] = None,
        issue_url: Annotated[
            str | None, Field(description="Issue to work on. Implies the repo.")
        ] = None,
        ref: Annotated[
            str | None, Field(description="Branch, tag or commit. Defaults to the default branch.")
        ] = None,
        install_command: Annotated[
            str | None, Field(description="Override the detected dependency install command.")
        ] = None,
        test_command: Annotated[
            str | None, Field(description="Override the detected test command.")
        ] = None,
        use_ci: Annotated[
            bool,
            Field(
                description="Read install/test/lint commands from the repo's CI workflow. "
                "Set false to use the language's default commands instead."
            ),
        ] = True,
    ) -> JobHandle:
        """Clone a repository into a fresh workspace on a new branch and build its playbook
        (how to build and test it, conventions, contribution policy). Runs in the
        background: poll get_job with the returned job_id. The job result holds `workspace`
        and `playbook`."""
        issue_number = issue_title = None
        if issue_url:
            source, issue_number = parse_issue_url(issue_url)
        elif repo:
            source = repo if repo.startswith(("/", "file://")) else parse_repo(repo)
        else:
            raise ValueError("Provide repo or issue_url.")

        async def work() -> dict[str, Any]:
            nonlocal issue_title
            if issue_number is not None:
                issue = await svc.github.get_or_none(f"/repos/{source}/issues/{issue_number}")
                issue_title = issue["title"] if issue else None
            ws = await svc.workspaces.create(
                source, ref=ref, issue_number=issue_number, issue_title=issue_title
            )
            playbook = await svc.build_playbook(
                ws,
                gh=svc.github,
                use_ci=use_ci,
                install_command=install_command,
                test_command=test_command,
            )
            return {
                "workspace": ws.model_dump(mode="json"),
                "playbook": playbook.model_dump(mode="json"),
            }

        return svc.jobs.start("workspace", work)

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def get_job(
        job_id: Annotated[str, Field(description="Handle returned by a start_* tool.")],
        wait_seconds: Annotated[
            float, Field(ge=0, le=60, description="Wait up to this long for the job to finish.")
        ] = 20,
    ) -> JobStatus:
        """Poll a background job. Returns state 'running', 'succeeded' (with `result`) or
        'failed' (with `error`). Call again while it is running."""
        return await svc.jobs.status(job_id, wait_s=wait_seconds)

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def list_workspaces() -> list[Workspace]:
        """List workspaces on this machine, with the state of any linked pull request."""
        return svc.workspaces.list()

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
    @user_errors
    async def delete_workspace(workspace_id: WorkspaceId) -> str:
        """Delete a workspace and its checkout from disk. Uncommitted work is lost."""
        svc.workspaces.delete(workspace_id)
        return f"Deleted {workspace_id}."

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def get_playbook(workspace_id: WorkspaceId) -> Playbook:
        """The repo's conventions: how to install, test and lint it (read from its CI),
        commit and pull request style, what past merged changes contained, and contribution
        policy (DCO, CLA, AI rules). Read it before editing."""
        return svc.playbook(workspace_id)

    @mcp.resource("mergemcp://workspaces/{workspace_id}/playbook", mime_type="application/json")
    async def playbook_resource(workspace_id: str) -> str:
        """The cached playbook for a workspace, as JSON."""
        return svc.playbook(workspace_id).model_dump_json(indent=2)

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def find_precedents(
        workspace_id: WorkspaceId,
        paths: Annotated[
            list[str],
            Field(min_length=1, description="Files or directories you expect to change."),
        ],
        limit: Annotated[int, Field(ge=1, le=10, description="Maximum commits.")] = 5,
    ) -> PrecedentList:
        """Show recent commits that changed the same files, with everything else each one
        touched (tests, changelog, docs). Use them as worked examples of how this project
        makes this kind of change."""
        for path in paths:
            svc.workspaces.resolve(workspace_id, path)
        return await precedents.find_precedents(
            svc.workspaces.checkout(workspace_id), paths, svc.rules, limit
        )

    # ------------------------------------------------------------------ files

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def list_files(
        workspace_id: WorkspaceId,
        prefix: Annotated[str, Field(description="Only paths under this directory.")] = "",
    ) -> FileList:
        """List files in the workspace (tracked and new, ignoring .gitignore'd paths)."""
        return await svc.workspaces.list_files(workspace_id, prefix)

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def read_file(
        workspace_id: WorkspaceId,
        path: RepoPath,
        start_line: Annotated[int, Field(ge=1, description="First line, 1-based.")] = 1,
        max_lines: Annotated[int, Field(ge=1, le=1000, description="Lines to return.")] = 400,
    ) -> FileContent:
        """Read a text file from the workspace, optionally a line range."""
        return svc.workspaces.read_file(workspace_id, path, start_line, max_lines)

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def search_code(
        workspace_id: WorkspaceId,
        pattern: Annotated[str, Field(description="Extended regular expression (git grep -E).")],
        path_prefix: Annotated[str, Field(description="Limit to this file or directory.")] = "",
    ) -> SearchResult:
        """Search file contents in the workspace. Returns path, line number and line text."""
        return await svc.workspaces.search(workspace_id, pattern, path_prefix)

    @mcp.tool(annotations=WRITES_WORKSPACE)
    @user_errors
    async def edit_file(
        workspace_id: WorkspaceId,
        path: RepoPath,
        old_text: Annotated[str, Field(description="Exact text to replace. Must occur once.")],
        new_text: Annotated[str, Field(description="Replacement text.")],
    ) -> WriteResult:
        """Replace one exact occurrence of old_text in a file. Fails if it matches zero or
        several times, so include enough surrounding lines to be unique."""
        return svc.workspaces.edit_file(workspace_id, path, old_text, new_text)

    @mcp.tool(annotations=WRITES_WORKSPACE)
    @user_errors
    async def write_file(
        workspace_id: WorkspaceId,
        path: RepoPath,
        content: Annotated[str, Field(description="Full new contents of the file.")],
    ) -> WriteResult:
        """Create or overwrite a file in the workspace. Prefer edit_file for small changes."""
        return svc.workspaces.write_file(workspace_id, path, content)

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def get_diff(workspace_id: WorkspaceId) -> DiffResult:
        """Show everything changed in the workspace since the base commit."""
        return await svc.workspaces.diff(workspace_id)

    # ------------------------------------------------------------------ checks

    @mcp.tool(annotations=WRITES_WORKSPACE)
    @user_errors
    async def start_check(
        workspace_id: WorkspaceId,
        kind: Annotated[
            CheckKind,
            Field(
                description="'setup' installs dependencies (run once, first). 'test' runs the "
                "repo's test suite. 'lint' runs its linter."
            ),
        ],
        test_ids: Annotated[
            list[str] | None,
            Field(
                description="For kind='test', when the playbook says accepts_test_ids: files "
                "or test ids to run, e.g. ['tests/test_x.py::test_y']. Omit to run everything."
            ),
        ] = None,
    ) -> JobHandle:
        """Run the repo's own setup, tests or linter in an isolated sandbox, using the
        commands from the playbook. Runs in the background: poll get_job. The result reports
        pass/fail, the end of the output and, where the test runner is understood, per-test
        outcomes."""
        svc.playbook(workspace_id)  # fail now, not inside the job, if the id is wrong

        async def work() -> CheckResult:
            return await svc.run_check(workspace_id, kind, test_ids)

        return svc.jobs.start(f"check:{kind}", work)

    # ------------------------------------------------------------------ handoff

    @mcp.tool(annotations=READ_ONLY)
    @user_errors
    async def check_readiness(workspace_id: WorkspaceId) -> Readiness:
        """Pre-flight the current diff: have tests and the linter passed on it, and does it
        contain what this repo's merged changes usually contain (tests, a changelog entry)?
        Returns the findings a reviewer would likely raise. Fix them before
        prepare_pull_request."""
        report, _ = await svc.readiness(workspace_id)
        return report

    @mcp.tool(annotations=WRITES_WORKSPACE)
    @user_errors
    async def prepare_pull_request(
        workspace_id: WorkspaceId,
        title: Annotated[
            str,
            Field(
                min_length=5, max_length=120, description="Pull request title in the repo's style."
            ),
        ],
        summary: Annotated[
            str, Field(min_length=10, description="What changed and why, for the maintainer.")
        ],
        testing_notes: Annotated[str, Field(description="How the change was verified.")] = "",
    ) -> PullRequestDraft:
        """Final step. Builds a pull request draft (title, body, checklist) and the commands
        the human must run to commit, push and open it. Nothing is submitted: show the
        checklist and commands to the user and stop."""
        ws = svc.workspaces.get(workspace_id)
        report, diff = await svc.readiness(workspace_id)
        if not diff.files_changed:
            raise ValueError("The workspace has no changes to submit.")
        return build_draft(
            ws,
            svc.playbook(workspace_id),
            diff,
            report,
            title=title,
            summary=summary,
            testing_notes=testing_notes,
            workspace_root=svc.workspaces.root(workspace_id),
        )

    @mcp.tool(annotations={**READ_ONLY_REMOTE, "readOnlyHint": False})
    @user_errors
    async def check_pull_request(
        pull_request_url: Annotated[str, Field(description="https://github.com/owner/name/pull/N")],
        workspace_id: Annotated[
            str | None,
            Field(
                description="Optional. Links the pull request to its workspace so "
                "list_workspaces shows its state."
            ),
        ] = None,
    ) -> PullRequestStatus:
        """After the human has opened the pull request: read its CI results and reviews and
        return what to do next to get it merged. It only reads from GitHub; it never
        comments or pushes."""
        repo, number = parse_issue_url(pull_request_url)
        status = await pr_status.check_pull_request(svc.github, repo, number)
        if workspace_id:
            svc.workspaces.update(
                workspace_id, pull_request_url=pull_request_url, pull_request_state=status.state
            )
        return status

    # ------------------------------------------------------------------ prompt

    @mcp.prompt
    def contribute(issue_url: str) -> str:
        """Guided workflow for contributing a fix for one GitHub issue."""
        return (
            f"Help me contribute a fix for {issue_url} using the merge-mcp tools.\n"
            "1. assess_issue first. If the verdict is 'avoid', stop and tell me why.\n"
            "2. start_workspace with the issue URL; poll get_job; read the playbook.\n"
            "3. find_precedents on the files you expect to change, and follow those examples.\n"
            "4. Run start_check(kind='setup'), then reproduce the problem with a failing test.\n"
            "5. Make the smallest change that fixes it, following the playbook's conventions.\n"
            "6. Run the tests and the linter until both pass on the final diff.\n"
            "7. Run check_readiness and fix what it flags.\n"
            "8. Call prepare_pull_request, show me the checklist and commands, and stop. "
            "I will review the diff and submit it myself."
        )

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
