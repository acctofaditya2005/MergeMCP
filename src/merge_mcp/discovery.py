"""Issue discovery and viability scoring.

The design rule: gather facts with I/O (`collect_signals`), then score them with a pure
function (`score`). No LLM is involved, so the same issue state always produces the same
score and every point is explained in `reasons`.
"""

from __future__ import annotations

import asyncio
import statistics
from datetime import UTC, datetime
from typing import Any

from .github import GitHubClient, GitHubError, RateLimited
from .models import (
    IssueAssessment,
    IssueCandidate,
    IssueSearchResult,
    Viability,
    ViabilitySignals,
)
from .rules import ViabilityRules


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _days_since(value: str | None, now: datetime) -> int | None:
    ts = _parse_ts(value)
    return None if ts is None else max(0, (now - ts).days)


def score(signals: ViabilitySignals, rules: ViabilityRules) -> Viability:
    """Turn signals into a 0-100 score. Pure and deterministic.

    Thresholds and weights come from the `[viability]` rules; this function only decides
    which rules fire.
    """
    s = signals
    if not s.issue_open:
        return Viability(score=0, verdict="avoid", reasons=["Issue is closed."], signals=s)
    if s.repo_archived:
        return Viability(score=0, verdict="avoid", reasons=["Repository is archived."], signals=s)

    fired: list[tuple[str, str]] = []
    if s.assignees:
        fired.append(("assigned", "Issue is already assigned."))
    if s.open_linked_prs:
        fired.append(
            (
                "open_linked_pr",
                f"{s.open_linked_prs} open pull request(s) already reference this issue.",
            )
        )
    if s.recent_claim_comments:
        fired.append(
            (
                "recent_claim",
                f"Someone said they are working on it in the last {rules.claim_window_days} days.",
            )
        )
    if (
        s.days_since_issue_activity is not None
        and s.days_since_issue_activity > rules.stale_issue_days
    ):
        fired.append(
            (
                "stale_issue",
                f"No activity on the issue for over "
                f"{rules.stale_issue_days} days; it may be obsolete.",
            )
        )
    if s.days_since_repo_push is not None and s.days_since_repo_push > rules.inactive_repo_days:
        fired.append(
            ("inactive_repo", f"Repository has had no pushes for {s.days_since_repo_push} days.")
        )
    if s.external_prs_merged_recently == 0:
        fired.append(
            ("no_external_merges", "No recently merged pull requests from outside contributors.")
        )
    if (
        s.median_days_to_merge_external is not None
        and s.median_days_to_merge_external > rules.slow_merge_days
    ):
        fired.append(
            (
                "slow_merges",
                f"Outside pull requests take a median of "
                f"{s.median_days_to_merge_external:.0f} days to merge.",
            )
        )
    if s.has_contributing_guide is False:
        fired.append(("no_contributing_guide", "No CONTRIBUTING guide found."))
    if s.newcomer_label:
        fired.append(("newcomer_label", "Labeled as suitable for new contributors."))

    points = 100
    reasons = []
    for key, why in fired:
        weight = rules.weights.get(key, 0)
        if weight:
            points += weight
            reasons.append(f"{weight:+d}: {why}")
    points = max(0, min(100, points))
    verdict = (
        "good" if points >= rules.good_at else "risky" if points >= rules.risky_at else "avoid"
    )
    return Viability(
        score=points, verdict=verdict, reasons=reasons or ["No warning signs found."], signals=s
    )


def build_query(
    *,
    language: str | None,
    labels: list[str],
    keywords: str | None,
    repo: str | None,
    min_stars: int | None,
    updated_within_days: int | None,
    now: datetime,
) -> str:
    parts = ["is:issue", "is:open", "no:assignee", "-linked:pr", "archived:false"]
    if repo:
        parts.append(f"repo:{repo}")
    if language:
        parts.append(f"language:{language}")
    for label in labels:
        parts.append(f'label:"{label}"')
    if min_stars and not repo:
        parts.append(f"stars:>={min_stars}")
    if updated_within_days:
        since = now.date().toordinal() - updated_within_days
        parts.append(f"updated:>={datetime.fromordinal(since).date().isoformat()}")
    if keywords:
        parts.append(keywords.strip())
    return " ".join(parts)


def _labels(issue: dict[str, Any]) -> list[str]:
    return [lb["name"] if isinstance(lb, dict) else str(lb) for lb in issue.get("labels", [])]


def _repo_from_issue(issue: dict[str, Any]) -> str:
    return issue["repository_url"].split("/repos/", 1)[1]


def _signals_from_issue(
    issue: dict[str, Any], now: datetime, rules: ViabilityRules
) -> ViabilitySignals:
    labels = {name.lower() for name in _labels(issue)}
    return ViabilitySignals(
        issue_open=issue.get("state") == "open",
        assignees=len(issue.get("assignees") or []),
        days_since_issue_activity=_days_since(issue.get("updated_at"), now),
        newcomer_label=bool(labels & {lb.lower() for lb in rules.newcomer_labels}),
    )


async def _repo_signals(
    gh: GitHubClient, repo: str, now: datetime, rules: ViabilityRules
) -> dict[str, Any]:
    """Repo-level facts: activity and how outside pull requests are treated."""
    info, pulls = await asyncio.gather(gh.repo(repo), gh.closed_pulls(repo))
    external = [
        p
        for p in pulls
        if p.get("merged_at") and p.get("author_association") not in rules.internal_associations
    ]
    days = [
        (_parse_ts(p["merged_at"]) - _parse_ts(p["created_at"])).total_seconds() / 86400
        for p in external
    ]
    return {
        "repo_archived": bool(info.get("archived")),
        "days_since_repo_push": _days_since(info.get("pushed_at"), now),
        "external_prs_merged_recently": len(external),
        "median_days_to_merge_external": round(statistics.median(days), 1) if days else None,
        "_stars": info.get("stargazers_count"),
    }


async def rank_issues(
    gh: GitHubClient, items: list[dict[str, Any]], rules: ViabilityRules, now: datetime
) -> tuple[list[IssueCandidate], str | None]:
    """Score search results. Repo-level signals are fetched once per repo, a few at a time."""
    repos = sorted({_repo_from_issue(i) for i in items})
    gate = asyncio.Semaphore(4)
    note: str | None = None

    async def fetch(name: str) -> tuple[str, dict[str, Any]]:
        async with gate:
            try:
                return name, await _repo_signals(gh, name, now, rules)
            except RateLimited:
                raise
            except GitHubError:
                return name, {}

    try:
        repo_facts = dict(await asyncio.gather(*(fetch(r) for r in repos)))
    except RateLimited as exc:
        repo_facts = {}
        note = f"Scores use issue data only. {exc}"

    blind = sum(1 for i in items if not repo_facts.get(_repo_from_issue(i)))
    if blind and note is None:
        note = (
            f"Repository signals were unavailable for {blind} candidate(s); their scores "
            "use issue data only. Run assess_issue before starting."
        )

    candidates = []
    for issue in items:
        name = _repo_from_issue(issue)
        facts = dict(repo_facts.get(name, {}))
        stars = facts.pop("_stars", None)
        signals = _signals_from_issue(issue, now, rules).model_copy(update=facts)
        candidates.append(
            IssueCandidate(
                repo=name,
                number=issue["number"],
                title=issue["title"],
                url=issue["html_url"],
                labels=_labels(issue),
                comments=issue.get("comments", 0),
                updated_at=issue["updated_at"],
                repo_stars=stars,
                viability=score(signals, rules),
            )
        )
    candidates.sort(key=lambda c: (-c.viability.score, c.repo, c.number))
    return candidates, note


async def find_issues(
    gh: GitHubClient,
    rules: ViabilityRules,
    *,
    language: str | None = None,
    labels: list[str] | None = None,
    keywords: str | None = None,
    repo: str | None = None,
    min_stars: int | None = 50,
    updated_within_days: int | None = 90,
    limit: int = 10,
    now: datetime | None = None,
) -> IssueSearchResult:
    now = now or datetime.now(UTC)
    labels = rules.search_labels[:1] if labels is None else labels
    query = build_query(
        language=language,
        labels=labels,
        keywords=keywords,
        repo=repo,
        min_stars=min_stars,
        updated_within_days=updated_within_days,
        now=now,
    )
    data = await gh.search_issues(query, per_page=min(max(limit * 2, 10), 50))
    items = [i for i in data.get("items", []) if "pull_request" not in i]
    candidates, note = await rank_issues(gh, items, rules, now)
    return IssueSearchResult(
        query=query,
        total_matches=data.get("total_count", 0),
        candidates=candidates[:limit],
        note=note,
    )


async def collect_signals(
    gh: GitHubClient, repo: str, number: int, now: datetime, rules: ViabilityRules
) -> tuple[dict[str, Any], ViabilitySignals]:
    issue, timeline, comments, repo_facts, has_guide = await asyncio.gather(
        gh.issue(repo, number),
        gh.get_or_none(f"/repos/{repo}/issues/{number}/timeline", per_page=100),
        gh.issue_comments(repo, number),
        _repo_signals(gh, repo, now, rules),
        _has_contributing(gh, repo),
    )
    if "pull_request" in issue:
        raise GitHubError(f"{repo}#{number} is a pull request, not an issue.")

    open_prs = set()
    for event in timeline or []:
        source = (event.get("source") or {}).get("issue") or {}
        if (
            event.get("event") == "cross-referenced"
            and "pull_request" in source
            and source.get("state") == "open"
        ):
            open_prs.add(source.get("number"))

    claims = sum(
        1
        for c in comments
        if c.get("author_association") not in rules.internal_associations
        and rules.claim_regex.search(c.get("body") or "")
        and (_days_since(c.get("created_at"), now) or 0) <= rules.claim_window_days
    )
    repo_facts.pop("_stars", None)
    signals = _signals_from_issue(issue, now, rules).model_copy(
        update={
            **repo_facts,
            "open_linked_prs": len(open_prs),
            "recent_claim_comments": claims,
            "has_contributing_guide": has_guide,
        }
    )
    return issue, signals


async def _has_contributing(gh: GitHubClient, repo: str) -> bool:
    for path in (
        "CONTRIBUTING.md",
        ".github/CONTRIBUTING.md",
        "docs/CONTRIBUTING.md",
        "CONTRIBUTING.rst",
    ):
        if await gh.has_file(repo, path):
            return True
    return False


async def assess_issue(
    gh: GitHubClient,
    repo: str,
    number: int,
    rules: ViabilityRules,
    now: datetime | None = None,
) -> IssueAssessment:
    now = now or datetime.now(UTC)
    issue, signals = await collect_signals(gh, repo, number, now, rules)
    body = (issue.get("body") or "").strip()
    return IssueAssessment(
        repo=repo,
        number=number,
        title=issue["title"],
        url=issue["html_url"],
        body_excerpt=body[:4000] + ("…" if len(body) > 4000 else ""),
        labels=_labels(issue),
        viability=score(signals, rules),
    )
