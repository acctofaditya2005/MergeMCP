from datetime import UTC, datetime

import httpx
import pytest

from merge_mcp import discovery
from merge_mcp.config import Settings
from merge_mcp.github import (
    GitHubClient,
    GitHubError,
    RateLimited,
    parse_issue_url,
    parse_repo,
)
from merge_mcp.models import ViabilitySignals
from merge_mcp.rules import load_rules

NOW = datetime(2026, 11, 1, tzinfo=UTC)
V = load_rules().viability


def make_client(handler) -> GitHubClient:
    return GitHubClient(Settings(github_token="t0ken"), transport=httpx.MockTransport(handler))


# ---- scoring: pure function ------------------------------------------------------


def test_clean_issue_scores_full_marks():
    v = discovery.score(ViabilitySignals(newcomer_label=True, external_prs_merged_recently=5), V)
    assert (v.score, v.verdict) == (100, "good")


def test_closed_or_archived_is_zero():
    assert discovery.score(ViabilitySignals(issue_open=False), V).score == 0
    assert discovery.score(ViabilitySignals(repo_archived=True), V).verdict == "avoid"


@pytest.mark.parametrize(
    ("signals", "expected", "verdict"),
    [
        ({"assignees": 1}, 40, "risky"),
        ({"open_linked_prs": 1}, 50, "risky"),
        ({"assignees": 1, "open_linked_prs": 2}, 0, "avoid"),
        ({"recent_claim_comments": 1}, 80, "good"),
        ({"days_since_repo_push": 400, "external_prs_merged_recently": 0}, 40, "risky"),
        ({"median_days_to_merge_external": 45.0, "has_contributing_guide": False}, 80, "good"),
    ],
)
def test_deductions(signals, expected, verdict):
    v = discovery.score(ViabilitySignals(**signals), V)
    assert (v.score, v.verdict) == (expected, verdict)
    assert all(r.startswith(("-", "+")) for r in v.reasons)  # every point is explained


def test_score_is_deterministic():
    s = ViabilitySignals(assignees=1, newcomer_label=True)
    assert discovery.score(s, V) == discovery.score(s, V)


def test_build_query():
    q = discovery.build_query(
        language="python",
        labels=["good first issue"],
        keywords="asyncio",
        repo=None,
        min_stars=100,
        updated_within_days=30,
        now=NOW,
    )
    assert q == (
        "is:issue is:open no:assignee -linked:pr archived:false language:python "
        'label:"good first issue" stars:>=100 updated:>=2026-10-02 asyncio'
    )


# ---- URL parsing -----------------------------------------------------------------


def test_parse_repo_and_issue_url():
    assert parse_repo("https://github.com/pallets/click.git") == "pallets/click"
    assert parse_repo("pallets/click") == "pallets/click"
    assert parse_issue_url("https://github.com/pallets/click/issues/12#x") == ("pallets/click", 12)
    for bad in ("https://gitlab.com/a/b", "a/b; rm -rf /", "../../etc"):
        with pytest.raises(ValueError):
            parse_repo(bad)
    with pytest.raises(ValueError):
        parse_issue_url("https://github.com/pallets/click")


# ---- API-backed paths with a mock transport --------------------------------------

ISSUE = {
    "number": 12,
    "title": "Crash on empty input",
    "state": "open",
    "assignees": [],
    "html_url": "https://github.com/o/r/issues/12",
    "body": "It crashes.",
    "labels": [{"name": "good first issue"}],
    "comments": 1,
    "updated_at": "2026-10-25T00:00:00Z",
    "repository_url": "https://api.github.com/repos/o/r",
}
REPO = {"archived": False, "pushed_at": "2026-10-30T00:00:00Z", "stargazers_count": 900}
PULLS = [
    {
        "merged_at": "2026-10-10T00:00:00Z",
        "created_at": "2026-10-08T00:00:00Z",
        "author_association": "CONTRIBUTOR",
        "title": "fix: x",
        "head": {"ref": "fix-x"},
    },
    {"merged_at": None, "created_at": "2026-10-01T00:00:00Z", "author_association": "NONE"},
    {
        "merged_at": "2026-10-12T00:00:00Z",
        "created_at": "2026-10-11T00:00:00Z",
        "author_association": "MEMBER",
    },
]


def router(overrides: dict | None = None):
    routes = {
        "/search/issues": {"total_count": 1, "items": [ISSUE]},
        "/repos/o/r": REPO,
        "/repos/o/r/pulls": PULLS,
        "/repos/o/r/issues/12": ISSUE,
        "/repos/o/r/issues/12/timeline": [],
        "/repos/o/r/issues/12/comments": [],
        "/repos/o/r/contents/CONTRIBUTING.md": {"name": "CONTRIBUTING.md"},
        **(overrides or {}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer t0ken"
        body = routes.get(request.url.path)
        return httpx.Response(404 if body is None else 200, json=body or {})

    return handler


async def test_find_issues_ranks_and_explains():
    result = await discovery.find_issues(make_client(router()), V, language="python", now=NOW)
    (c,) = result.candidates
    assert (c.repo, c.number, c.repo_stars) == ("o/r", 12, 900)
    assert c.viability.score == 100
    assert c.viability.signals.external_prs_merged_recently == 1
    assert c.viability.signals.median_days_to_merge_external == 2.0
    assert "no:assignee" in result.query


async def test_find_issues_says_when_repo_signals_are_missing():
    gh = make_client(router({"/repos/o/r": None, "/repos/o/r/pulls": None}))
    result = await discovery.find_issues(gh, V, now=NOW)
    assert result.candidates[0].viability.signals.external_prs_merged_recently is None
    assert "unavailable for 1 candidate" in result.note


async def test_assess_issue_detects_competing_pr_and_claims():
    timeline = [
        {
            "event": "cross-referenced",
            "source": {"issue": {"number": 40, "state": "open", "pull_request": {}}},
        }
    ]
    comments = [
        {
            "author_association": "NONE",
            "body": "Can I work on this?",
            "created_at": "2026-10-28T00:00:00Z",
        }
    ]
    gh = make_client(
        router(
            {"/repos/o/r/issues/12/timeline": timeline, "/repos/o/r/issues/12/comments": comments}
        )
    )
    a = await discovery.assess_issue(gh, "o/r", 12, V, now=NOW)
    assert a.viability.signals.open_linked_prs == 1
    assert a.viability.signals.recent_claim_comments == 1
    assert (a.viability.score, a.viability.verdict) == (40, "risky")


async def test_assess_rejects_pull_requests():
    gh = make_client(router({"/repos/o/r/issues/12": {**ISSUE, "pull_request": {}}}))
    with pytest.raises(GitHubError, match="pull request"):
        await discovery.assess_issue(gh, "o/r", 12, V, now=NOW)


async def test_rate_limit_is_a_clear_error():
    def handler(request):
        return httpx.Response(
            403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"}, json={}
        )

    with pytest.raises(RateLimited, match="read-only token"):
        await make_client(handler).repo("o/r")


async def test_server_errors_are_retried():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(502 if calls["n"] < 3 else 200, json=REPO)

    assert (await make_client(handler).repo("o/r"))["stargazers_count"] == 900
    assert calls["n"] == 3


def test_weights_come_from_rules_not_code():
    """Changing a weight in the rules file changes the score with no code change."""
    strict = V.model_copy(update={"weights": {**V.weights, "assigned": -100}})
    assert discovery.score(ViabilitySignals(assignees=1), V).score == 40
    assert discovery.score(ViabilitySignals(assignees=1), strict).score == 0
    lenient = V.model_copy(update={"good_at": 30})
    assert discovery.score(ViabilitySignals(assignees=1), lenient).verdict == "good"
