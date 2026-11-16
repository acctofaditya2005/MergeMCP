"""The human-in-the-loop gate.

`prepare_pull_request` is the last thing the server can do. It assembles a draft (title,
body, checklist, and the exact commands to run) and stops. There is no tool that commits,
pushes, forks or opens a pull request, and the GitHub token is never given write scope,
so the gate holds even if a model ignores its instructions.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from .models import (
    ChecklistItem,
    DiffResult,
    Playbook,
    PullRequestDraft,
    Readiness,
    Workspace,
)

AI_DISCLOSURE = (
    "This change was drafted with AI assistance (merge-mcp). I have reviewed every line, "
    "run the tests locally, and take responsibility for it."
)
_CONVENTIONAL = re.compile(r"^[a-z]+(\([^)]+\))?!?: \S")


_CHECK_LABELS = {
    "test": "Test suite passes on this exact diff",
    "lint": "Linter passes (`{command}`)",
}
_CHECK_DETAILS = {
    "not-run": ("todo", "Not run yet."),
    "stale": ("warning", "Last run was on an older version of the diff."),
    "failing": ("warning", "Last run failed."),
    "partial": ("warning", "Only a subset of tests was run last time."),
}
#: Readiness findings that restate a human-only step already listed below.
_HUMAN_CODES = {"dco", "cla", "ai-policy", "pre-commit"}


def build_draft(
    ws: Workspace,
    playbook: Playbook,
    diff: DiffResult,
    readiness: Readiness,
    *,
    title: str,
    summary: str,
    testing_notes: str,
    workspace_root: Path,
    disclose_ai: bool = True,
) -> PullRequestDraft:
    title = " ".join(title.split())
    policy = playbook.policy
    checklist: list[ChecklistItem] = []
    by_code = {f.code: f for f in readiness.findings}

    # Automated checks: status comes from recorded results, never from what the model says.
    for kind, label in _CHECK_LABELS.items():
        if kind == "lint" and not playbook.tooling.lint_command:
            continue
        item = label.format(command=playbook.tooling.lint_command)
        problem = next((suffix for suffix in _CHECK_DETAILS if f"{kind}-{suffix}" in by_code), None)
        if problem is None:
            checklist.append(ChecklistItem(item=item, status="done"))
        else:
            status, detail = _CHECK_DETAILS[problem]
            checklist.append(ChecklistItem(item=item, status=status, detail=detail))  # type: ignore[arg-type]

    # What this repo's history says a complete change contains.
    for finding in readiness.findings:
        if finding.code.split("-")[0] in _CHECK_LABELS or finding.code in _HUMAN_CODES:
            continue
        checklist.append(
            ChecklistItem(item=finding.message, status="warning", detail=finding.evidence)
        )

    commits = playbook.commits
    if commits and commits.style == "conventional" and not _CONVENTIONAL.match(title):
        example = commits.examples[0] if commits.examples else "fix: short description"
        checklist.append(
            ChecklistItem(
                item="Title follows the repo's commit style",
                status="warning",
                detail=f"This repo uses Conventional Commits, e.g. '{example}'.",
            )
        )
    if ws.issue_number is None:
        checklist.append(
            ChecklistItem(
                item="Pull request references an issue",
                status="warning",
                detail="No issue is linked to this workspace.",
            )
        )
    if policy.requires_dco_signoff:
        checklist.append(
            ChecklistItem(
                item="Commit is signed off (DCO)",
                status="todo",
                detail="The commit command below includes -s.",
            )
        )
    if policy.requires_cla:
        checklist.append(
            ChecklistItem(
                item="Contributor License Agreement signed",
                status="todo",
                detail="Only you can do this.",
            )
        )
    if policy.ai_policy_mentions:
        checklist.append(
            ChecklistItem(
                item="Complies with the project's AI contribution policy",
                status="todo",
                detail=" | ".join(policy.ai_policy_mentions[:2]),
            )
        )
    if playbook.tooling.uses_pre_commit:
        checklist.append(
            ChecklistItem(
                item="pre-commit hooks pass",
                status="todo",
                detail="Run `pre-commit run --all-files`.",
            )
        )
    checklist.append(
        ChecklistItem(
            item="I have read the full diff and understand every change",
            status="todo",
            detail="Required. Maintainers will ask you about it, not the tool.",
        )
    )

    parts = ["## Summary", summary.strip()]
    if ws.issue_number is not None:
        parts += ["", f"Fixes #{ws.issue_number}"]
    parts += ["", "## Testing", testing_notes.strip() or "_Describe how you verified this._"]
    if disclose_ai:
        parts += ["", "## Disclosure", AI_DISCLOSURE]
    template_path = policy.pr_template_path
    if template_path and (Path(ws.path) / template_path).is_file():
        template = (Path(ws.path) / template_path).read_text(encoding="utf-8", errors="replace")
        parts += [
            "",
            "---",
            "<!-- The project's pull request template follows. Fill it in and "
            "delete what does not apply. -->",
            template.strip(),
        ]
    body = "\n".join(parts) + "\n"

    body_path = workspace_root / "PR_DRAFT.md"
    body_path.write_text(body, encoding="utf-8")

    signoff = " -s" if policy.requires_dco_signoff else ""
    body_arg = shlex.quote(str(body_path))
    commands = [
        f"cd {shlex.quote(ws.path)}",
        f"git diff {ws.base_commit[:12]}    # review every line first",
        "git add -A",
        f"git commit{signoff} -m {shlex.quote(title)}",
        "gh repo fork --remote    # your fork becomes 'origin', the project becomes 'upstream'",
        f"git push -u origin {shlex.quote(ws.branch)}",
        f"gh pr create --draft --title {shlex.quote(title)} --body-file {body_arg}",
    ]
    return PullRequestDraft(
        workspace_id=ws.workspace_id,
        branch=ws.branch,
        title=title,
        body_path=str(body_path),
        body=body,
        files_changed=diff.files_changed,
        checklist=checklist,
        commands=commands,
    )
