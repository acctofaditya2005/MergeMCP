"""Typed inputs and outputs for every tool.

These Pydantic models are the public contract of the server: FastMCP turns them into
the JSON Schemas an MCP client sees. Keeping them in one module makes the contract
reviewable in one place and stops tool functions from returning ad-hoc dicts.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- discovery

Verdict = Literal["good", "risky", "avoid"]


class ViabilitySignals(BaseModel):
    """Raw facts about an issue and its repo. Scoring is a pure function of this."""

    issue_open: bool = True
    assignees: int = 0
    open_linked_prs: int = 0
    recent_claim_comments: int = 0
    days_since_issue_activity: int | None = None
    newcomer_label: bool = False
    repo_archived: bool = False
    days_since_repo_push: int | None = None
    external_prs_merged_recently: int | None = None
    median_days_to_merge_external: float | None = None
    has_contributing_guide: bool | None = None


class Viability(BaseModel):
    score: int = Field(ge=0, le=100, description="0 (do not start) to 100 (clear to start).")
    verdict: Verdict
    reasons: list[str] = Field(description="Every deduction or bonus, in plain words.")
    signals: ViabilitySignals


class IssueCandidate(BaseModel):
    repo: str = Field(description="owner/name")
    number: int
    title: str
    url: str
    labels: list[str]
    comments: int
    updated_at: str
    repo_stars: int | None = None
    viability: Viability


class IssueSearchResult(BaseModel):
    query: str = Field(description="The exact GitHub search query that was run.")
    total_matches: int
    candidates: list[IssueCandidate]
    note: str | None = None


class IssueAssessment(BaseModel):
    repo: str
    number: int
    title: str
    url: str
    body_excerpt: str
    labels: list[str]
    viability: Viability


# --------------------------------------------------------------------------- playbook


class Tooling(BaseModel):
    """How to install, test and lint this repo, and where that knowledge came from."""

    ecosystem: str | None = Field(default=None, description="Matched [ecosystems.*] rule.")
    toolchain_version: str | None = None
    image: str | None = Field(default=None, description="Sandbox image for this repo.")
    source: str = Field(
        default="defaults",
        description="'ci: <workflow> (job: <id>)', 'defaults: <ecosystem>', 'override' or 'none'.",
    )
    python_requires: str | None = None
    env_setup: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    install_command: str
    test_command: str
    lint_command: str | None = None
    test_flags: str = ""
    accepts_test_ids: bool = False
    result_parser: str | None = None
    detected_from: list[str] = Field(description="Files the commands were derived from.")
    uses_pre_commit: bool = False
    skipped_ci_steps: list[str] = Field(
        default_factory=list,
        description="CI commands that cannot run in the sandbox (secrets, sudo, other OS).",
    )


class CommitConventions(BaseModel):
    sample_size: int
    conventional_commits_ratio: float = Field(description="Share like 'fix(scope): message'.")
    scoped_prefix_ratio: float = Field(description="Share like 'module: message'.")
    median_subject_length: int
    signed_off_ratio: float
    style: Literal["conventional", "scoped-prefix", "free-form"]
    examples: list[str]


class PullRequestConventions(BaseModel):
    sample_size: int
    title_examples: list[str]
    branch_examples: list[str]
    median_files_changed: int | None = None
    references_issue_ratio: float


class ContributionPolicy(BaseModel):
    contributing_path: str | None = None
    pr_template_path: str | None = None
    code_of_conduct: bool = False
    requires_dco_signoff: bool = False
    requires_cla: bool = False
    ai_policy_mentions: list[str] = Field(
        default_factory=list,
        description="Lines in contributor docs that mention AI/LLM-generated contributions.",
    )
    key_sections: list[str] = Field(default_factory=list)


class CoChangeRule(BaseModel):
    """Learned from history: when `when` files change, `expect` files usually change too."""

    when: str
    expect: str
    support: int = Field(description="Commits that changed `when` files.")
    confidence: float = Field(description="Share of those that also changed `expect` files.")


class Precedents(BaseModel):
    sample_size: int
    rules: list[CoChangeRule]
    changelog_hint: str | None = Field(
        default=None, description="Where changelog entries usually go, e.g. 'changes/'."
    )
    median_files_changed: int | None = None


class PrecedentCommit(BaseModel):
    sha: str
    subject: str
    files: list[str]
    categories: list[str]


class PrecedentList(BaseModel):
    paths: list[str]
    commits: list[PrecedentCommit]


class Playbook(BaseModel):
    """How this repo expects contributions to look. Built once, cached per workspace."""

    repo: str
    base_commit: str
    tooling: Tooling
    commits: CommitConventions | None = None
    pull_requests: PullRequestConventions | None = None
    precedents: Precedents | None = None
    policy: ContributionPolicy
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- workspace


class Workspace(BaseModel):
    workspace_id: str = Field(description="Handle to pass to every other workspace tool.")
    repo: str
    source_url: str
    base_commit: str
    branch: str
    issue_number: int | None = None
    path: str = Field(description="Absolute path of the checkout on the server's machine.")
    created_at: str
    pull_request_url: str | None = Field(
        default=None, description="Set once the human has opened the pull request."
    )
    pull_request_state: str | None = None


class FileList(BaseModel):
    files: list[str]
    truncated: bool


class FileContent(BaseModel):
    path: str
    start_line: int
    end_line: int
    total_lines: int
    content: str


class SearchMatch(BaseModel):
    path: str
    line: int
    text: str


class SearchResult(BaseModel):
    matches: list[SearchMatch]
    truncated: bool


class WriteResult(BaseModel):
    path: str
    bytes_written: int
    created: bool


class DiffResult(BaseModel):
    base_commit: str
    files_changed: list[str]
    insertions: int
    deletions: int
    diff: str
    truncated: bool


# --------------------------------------------------------------------------- jobs/checks

JobState = Literal["running", "succeeded", "failed"]
CheckKind = Literal["setup", "test", "lint"]


class JobHandle(BaseModel):
    job_id: str = Field(description="Pass to get_job to poll for the result.")
    kind: str
    state: JobState


class JobStatus(BaseModel):
    job_id: str
    kind: str
    state: JobState
    elapsed_s: float
    result: dict[str, Any] | None = Field(
        default=None, description="Present when state is 'succeeded'. Shape depends on kind."
    )
    error: str | None = None


class TestOutcome(BaseModel):
    passed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    errored: list[str] = Field(default_factory=list)
    skipped: int = 0


class CheckResult(BaseModel):
    kind: CheckKind
    command: str
    ok: bool
    exit_code: int
    timed_out: bool
    duration_s: float
    sandbox: str
    tests: TestOutcome | None = None
    output_tail: str


# --------------------------------------------------------------------------- readiness

Severity = Literal["blocker", "warning", "info"]


class Finding(BaseModel):
    code: str = Field(description="Stable identifier, e.g. 'missing-tests'.")
    severity: Severity
    message: str
    evidence: str | None = None


class Readiness(BaseModel):
    """Would a reviewer bounce this diff? Computed by rules from the diff and the playbook."""

    ready: bool = Field(description="True when there are no blockers or warnings.")
    files_by_category: dict[str, list[str]]
    findings: list[Finding]


# --------------------------------------------------------------------------- follow-up


class CheckRun(BaseModel):
    name: str
    conclusion: str | None
    url: str | None = None


class ReviewComment(BaseModel):
    author: str
    path: str | None = None
    line: int | None = None
    excerpt: str


class PullRequestStatus(BaseModel):
    repo: str
    number: int
    title: str
    state: Literal["open", "closed", "merged"]
    draft: bool
    mergeable_state: str | None = None
    failing_checks: list[CheckRun]
    pending_checks: int
    passing_checks: int
    approved_by: list[str]
    changes_requested_by: list[str]
    recent_review_comments: list[ReviewComment]
    next_actions: list[str] = Field(description="What the contributor should do now, in order.")


# --------------------------------------------------------------------------- dependencies


class DependencyRepo(BaseModel):
    package: str
    ecosystem: str
    repo: str | None = Field(description="owner/name on GitHub, when it could be resolved.")


class DependencyIssueResult(BaseModel):
    packages_seen: int
    resolved: list[DependencyRepo]
    unresolved: list[str]
    queries: list[str]
    candidates: list[IssueCandidate]
    note: str | None = None


# --------------------------------------------------------------------------- handoff


class ChecklistItem(BaseModel):
    item: str
    status: Literal["done", "todo", "warning"]
    detail: str | None = None


class PullRequestDraft(BaseModel):
    """Everything a human needs to review and submit. Nothing here has been pushed."""

    workspace_id: str
    branch: str
    title: str
    body_path: str = Field(description="File holding the PR body, outside the repo checkout.")
    body: str
    files_changed: list[str]
    checklist: list[ChecklistItem]
    commands: list[str] = Field(description="Commands for the human to run, in order.")
    submitted: Literal[False] = False
