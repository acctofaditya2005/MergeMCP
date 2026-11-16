"""Follow a pull request after the human has opened it.

Most first pull requests are not rejected; they stall, because CI is red or a reviewer
asked for something and nobody followed up. `check_pull_request` reads the PR's state and
turns it into an ordered list of what to do next. It is read-only like everything else.
"""

from __future__ import annotations

import asyncio

from .github import GitHubClient
from .models import CheckRun, PullRequestStatus, ReviewComment

_FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_PASSED = {"success", "neutral", "skipped"}


def next_actions(
    *,
    state: str,
    draft: bool,
    mergeable_state: str | None,
    failing: list[CheckRun],
    pending: int,
    changes_requested: list[str],
    approved: list[str],
    comments: int,
) -> list[str]:
    """Pure decision logic: what should the contributor do now, most urgent first."""
    if state == "merged":
        return ["Merged. Nothing left to do."]
    if state == "closed":
        return ["Closed without merging. Read the closing comment before trying again."]
    actions = []
    if mergeable_state == "dirty":
        actions.append(
            "Resolve merge conflicts: rebase the branch on the project's default branch."
        )
    if failing:
        names = ", ".join(c.name for c in failing[:4])
        actions.append(
            f"Fix failing checks: {names}. Open each check's log, reproduce the "
            "failure locally with start_check, then push the fix."
        )
    if changes_requested:
        actions.append(
            f"Address the changes requested by {', '.join(changes_requested)}, "
            "then reply to each comment and re-request review."
        )
    elif comments and not approved:
        actions.append("Reply to the open review comments, even if only to confirm a change.")
    if not actions and pending:
        actions.append(f"Wait for {pending} check(s) still running.")
    if not actions and draft:
        actions.append("Checks are green. Mark the pull request as ready for review.")
    if not actions and approved:
        actions.append(
            "Approved and green. Wait for a maintainer to merge; do not ping "
            "unless it has been quiet for a week or more."
        )
    if not actions:
        actions.append(
            "Waiting for a first review. Be patient; a polite ping after one to "
            "two weeks is reasonable."
        )
    return actions


async def check_pull_request(gh: GitHubClient, repo: str, number: int) -> PullRequestStatus:
    pr = await gh.pull(repo, number)
    sha = pr["head"]["sha"]
    reviews, comments, checks = await asyncio.gather(
        gh.get(f"/repos/{repo}/pulls/{number}/reviews", per_page=100),
        gh.get(f"/repos/{repo}/pulls/{number}/comments", per_page=100),
        gh.get_or_none(f"/repos/{repo}/commits/{sha}/check-runs", per_page=100),
    )
    author = (pr.get("user") or {}).get("login")

    # A reviewer's latest review is the one that counts.
    latest: dict[str, str] = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login")
        if login and review.get("state") in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            latest[login] = review["state"]
    approved = sorted(u for u, s in latest.items() if s == "APPROVED")
    changes = sorted(u for u, s in latest.items() if s == "CHANGES_REQUESTED")

    runs = (checks or {}).get("check_runs", [])
    failing = [
        CheckRun(name=r["name"], conclusion=r.get("conclusion"), url=r.get("html_url"))
        for r in runs
        if r.get("conclusion") in _FAILED
    ]
    pending = sum(1 for r in runs if r.get("status") != "completed")
    passing = sum(1 for r in runs if r.get("conclusion") in _PASSED)

    theirs = [c for c in comments if (c.get("user") or {}).get("login") != author]
    recent = [
        ReviewComment(
            author=(c.get("user") or {}).get("login", "?"),
            path=c.get("path"),
            line=c.get("line") or c.get("original_line"),
            excerpt=" ".join((c.get("body") or "").split())[:240],
        )
        for c in theirs[-10:]
    ]

    state = "merged" if pr.get("merged") else pr["state"]
    return PullRequestStatus(
        repo=repo,
        number=number,
        title=pr["title"],
        state=state,
        draft=bool(pr.get("draft")),
        mergeable_state=pr.get("mergeable_state"),
        failing_checks=failing,
        pending_checks=pending,
        passing_checks=passing,
        approved_by=approved,
        changes_requested_by=changes,
        recent_review_comments=recent,
        next_actions=next_actions(
            state=state,
            draft=bool(pr.get("draft")),
            mergeable_state=pr.get("mergeable_state"),
            failing=failing,
            pending=pending,
            changes_requested=changes,
            approved=approved,
            comments=len(theirs),
        ),
    )
