"""Dependency-based issue matching and pull request follow-up (mocked network)."""

import httpx
import pytest

from merge_mcp import deps, pr_status
from merge_mcp.config import Settings
from merge_mcp.github import GitHubClient
from merge_mcp.models import CheckRun

# ---- manifests ---------------------------------------------------------------------


def test_manifest_parsers(rules):
    def names(filename, content):
        return deps.parse_manifest(filename, content, rules)

    assert names("requirements-dev.txt", "httpx>=0.27\n# c\n-r base.txt\nrich[jupyter]==13\n") == [
        ("python", "httpx"),
        ("python", "rich"),
    ]
    assert names(
        "sub/pyproject.toml",
        """
[project]
dependencies = ["pydantic>=2", "click"]
[project.optional-dependencies]
dev = ["pytest"]
[tool.poetry.dependencies]
python = "^3.11"
attrs = "*"
""",
    ) == [("python", "attrs"), ("python", "pydantic"), ("python", "click"), ("python", "pytest")]
    assert names(
        "package.json", '{"dependencies": {"express": "^4"}, "devDependencies": {"vitest": "^2"}}'
    ) == [("node", "express"), ("node", "vitest")]
    assert names(
        "go.mod",
        "module x\n\nrequire (\n\tgithub.com/spf13/cobra v1.8.0\n"
        "\tgolang.org/x/sync v0.7.0 // indirect\n)\n",
    ) == [("go", "github.com/spf13/cobra"), ("go", "golang.org/x/sync")]
    assert names(
        "Cargo.toml", '[dependencies]\nserde = "1"\n[dev-dependencies]\ninsta = "1"\n'
    ) == [("rust", "serde"), ("rust", "insta")]


def test_unknown_or_broken_manifest_is_a_clear_error(rules):
    with pytest.raises(ValueError, match="Unrecognised manifest 'Gemfile'"):
        deps.parse_manifest("Gemfile", "", rules)
    with pytest.raises(ValueError, match="Could not parse package.json"):
        deps.parse_manifest("package.json", "{not json", rules)


def test_github_repo_extraction():
    repo = deps.github_repo_from
    assert repo("https://github.com/encode/httpx") == "encode/httpx"
    assert repo("git+https://github.com/expressjs/express.git") == "expressjs/express"
    assert (
        repo({"type": "git", "url": "git@github.com:vitest-dev/vitest.git"}) == "vitest-dev/vitest"
    )
    assert repo("https://github.com/pallets/click/issues") == "pallets/click"
    assert repo("https://example.com/project") is None and repo(None) is None


REGISTRY = {
    "https://pypi.org/pypi/httpx/json": {
        "info": {"project_urls": {"Source": "https://github.com/encode/httpx"}}
    },
    "https://pypi.org/pypi/private-thing/json": None,
    "https://registry.npmjs.org/express": {
        "repository": {"type": "git", "url": "git+https://github.com/expressjs/express.git"}
    },
    "https://registry.npmjs.org/no-repo": {"name": "no-repo"},
}


def registry_client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = REGISTRY.get(str(request.url))
        return httpx.Response(404 if body is None else 200, json=body or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_resolve_repo_uses_registry_fields_from_rules(rules):
    http = registry_client()
    assert await deps.resolve_repo(http, "python", "httpx", rules) == "encode/httpx"
    assert await deps.resolve_repo(http, "node", "express", rules) == "expressjs/express"
    assert await deps.resolve_repo(http, "python", "private-thing", rules) is None
    assert await deps.resolve_repo(http, "node", "no-repo", rules) is None
    # Go module paths name the repo directly; no registry call is made.
    assert await deps.resolve_repo(http, "go", "github.com/spf13/cobra", rules) == "spf13/cobra"


async def test_find_issues_in_dependencies_end_to_end(rules):
    seen = {}

    def github(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/issues":
            seen["q"] = request.url.params["q"]
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "items": [
                        {
                            "number": 5,
                            "title": "Docs typo",
                            "state": "open",
                            "assignees": [],
                            "html_url": "https://github.com/encode/httpx/issues/5",
                            "comments": 0,
                            "labels": [{"name": "good first issue"}],
                            "updated_at": "2026-10-30T00:00:00Z",
                            "repository_url": "https://api.github.com/repos/encode/httpx",
                        }
                    ],
                },
            )
        if request.url.path == "/repos/encode/httpx":
            return httpx.Response(
                200,
                json={
                    "archived": False,
                    "stargazers_count": 13000,
                    "pushed_at": "2026-10-30T00:00:00Z",
                },
            )
        return httpx.Response(200, json=[])

    gh = GitHubClient(Settings(), transport=httpx.MockTransport(github))
    result = await deps.find_issues_in_dependencies(
        gh,
        registry_client(),
        rules,
        [
            ("python", "httpx"),
            ("node", "express"),
            ("python", "private-thing"),
            ("python", "httpx"),
        ],
    )
    assert result.packages_seen == 3  # duplicates collapse
    assert {r.repo for r in result.resolved} == {"encode/httpx", "expressjs/express"}
    assert result.unresolved == ["private-thing"]
    assert "repo:encode/httpx repo:expressjs/express" in seen["q"]
    assert 'label:"good first issue","help wanted"' in seen["q"]
    assert "no:assignee" in seen["q"] and "-linked:pr" in seen["q"]
    assert [(c.repo, c.number) for c in result.candidates] == [("encode/httpx", 5)]
    assert result.candidates[0].repo_stars == 13000


# ---- pull request follow-up --------------------------------------------------------

FAIL = [CheckRun(name="tests (3.12)", conclusion="failure")]


def actions(**overrides) -> list[str]:
    base = {
        "state": "open",
        "draft": False,
        "mergeable_state": "clean",
        "failing": [],
        "pending": 0,
        "changes_requested": [],
        "approved": [],
        "comments": 0,
    }
    return pr_status.next_actions(**{**base, **overrides})


def test_next_actions_priorities():
    assert actions(state="merged") == ["Merged. Nothing left to do."]
    assert "Closed without merging" in actions(state="closed")[0]
    ordered = actions(mergeable_state="dirty", failing=FAIL, changes_requested=["maintainer"])
    assert [a.split(":")[0].split(" by ")[0] for a in ordered] == [
        "Resolve merge conflicts",
        "Fix failing checks",
        "Address the changes requested",
    ]
    assert "tests (3.12)" in ordered[1]
    assert "Reply to the open review comments" in actions(comments=2)[0]
    assert actions(pending=3) == ["Wait for 3 check(s) still running."]
    assert "ready for review" in actions(draft=True)[0]
    assert "Approved and green" in actions(approved=["maintainer"])[0]
    assert "Waiting for a first review" in actions()[0]


async def test_check_pull_request_reads_reviews_and_checks():
    routes = {
        "/repos/o/r/pulls/9": {
            "title": "fix: tabs",
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable_state": "blocked",
            "user": {"login": "me"},
            "head": {"sha": "abc"},
        },
        "/repos/o/r/pulls/9/reviews": [
            {"user": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "bob"}, "state": "APPROVED"},
            {"user": {"login": "alice"}, "state": "APPROVED"},  # her later review wins
            {"user": {"login": "carol"}, "state": "CHANGES_REQUESTED"},
            {"user": {"login": "dave"}, "state": "COMMENTED"},
        ],
        "/repos/o/r/pulls/9/comments": [
            {
                "user": {"login": "carol"},
                "path": "src/a.py",
                "line": 12,
                "body": "Please add\n a test for this.",
            },
            {"user": {"login": "me"}, "path": "src/a.py", "line": 12, "body": "Will do."},
        ],
        "/repos/o/r/commits/abc/check-runs": {
            "check_runs": [
                {"name": "lint", "status": "completed", "conclusion": "success"},
                {"name": "tests", "status": "completed", "conclusion": "failure", "html_url": "u"},
                {"name": "docs", "status": "in_progress", "conclusion": None},
            ]
        },
    }
    gh = GitHubClient(
        Settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=routes[request.url.path])
        ),
    )
    status = await pr_status.check_pull_request(gh, "o/r", 9)
    assert status.state == "open"
    assert (status.approved_by, status.changes_requested_by) == (["alice", "bob"], ["carol"])
    assert [c.name for c in status.failing_checks] == ["tests"]
    assert (status.passing_checks, status.pending_checks) == (1, 1)
    assert [(c.author, c.excerpt) for c in status.recent_review_comments] == [
        ("carol", "Please add a test for this.")
    ]  # my own replies are not action items
    assert status.next_actions[0].startswith("Fix failing checks: tests")
    assert "carol" in status.next_actions[1]
