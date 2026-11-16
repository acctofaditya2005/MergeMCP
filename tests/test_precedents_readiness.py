"""Learning what a complete change looks like, and checking a diff against it."""

import subprocess

import pytest

from merge_mcp import precedents
from merge_mcp.models import (
    CoChangeRule,
    ContributionPolicy,
    Playbook,
    Precedents,
    Tooling,
)
from merge_mcp.readiness import assess

R, F = "\x1e", "\x1f"


def log(*commits: tuple[str, list[str]]) -> str:
    return "".join(
        f"{R}{i:040x}{F}{subject}\n\n" + "\n".join(files) + "\n"
        for i, (subject, files) in enumerate(commits)
    )


def history(n_with_tests: int, n_without: int, changelog: str | None = "changes/") -> str:
    commits = []
    for i in range(n_with_tests):
        files = [f"src/mod{i}.go", f"src/mod{i}_test.go"]
        if changelog:
            files.append(f"{changelog}{i}.fix.md")
        commits.append((f"fix: thing {i}", files))
    commits += [(f"refactor: tidy {i}", [f"src/other{i}.go"]) for i in range(n_without)]
    commits += [
        ("Bump actions/checkout from 3 to 4", [".github/workflows/ci.yml"]),
        ("Merge branch 'main'", ["src/x.go"]),
    ]
    return log(*commits)


# ---- learning ----------------------------------------------------------------------


def test_learns_strong_rules_and_ignores_noise(rules):
    learned = precedents.learn(precedents.parse_log(history(9, 1)), rules)
    assert learned.sample_size == 10  # the Bump and Merge commits are not evidence
    by_expect = {r.expect: r for r in learned.rules}
    assert by_expect["tests"].confidence == 0.9 and by_expect["tests"].support == 10
    assert by_expect["changelog"].confidence == 0.9
    assert "docs" not in by_expect
    assert learned.changelog_hint == "changes/"
    assert learned.median_files_changed == 3


def test_weak_patterns_are_not_reported(rules):
    learned = precedents.learn(precedents.parse_log(history(4, 6, changelog=None)), rules)
    assert learned.rules == []  # 40% is below min_confidence
    assert learned.changelog_hint is None


def test_too_little_history_yields_no_rules(rules):
    learned = precedents.learn(precedents.parse_log(history(5, 0)), rules)
    assert learned.rules == []  # 5 commits is below min_support
    assert precedents.learn([], rules) is None


def test_single_changelog_file_is_the_hint(rules):
    commits = [(f"fix {i}", [f"lib/a{i}.rb", "CHANGELOG.md"]) for i in range(10)]
    assert (
        precedents.learn(precedents.parse_log(log(*commits)), rules).changelog_hint
        == "CHANGELOG.md"
    )


def test_thresholds_come_from_rules(rules):
    strict = rules.model_copy(
        update={"precedents": rules.precedents.model_copy(update={"min_confidence": 0.95})}
    )
    assert precedents.learn(precedents.parse_log(history(9, 1)), strict).rules == []


async def test_find_precedents_on_a_real_repo(services, fixture_repo, rules):
    ws = await services.workspaces.create(fixture_repo["path"], ref=fixture_repo["head"])
    checkout = services.workspaces.checkout(ws.workspace_id)
    found = await precedents.find_precedents(checkout, ["textkit.py"], rules)
    assert [c.subject for c in found.commits] == [
        "fix: strip punctuation in slugify (#7)",
        "feat: add slugify and truncate",
    ]
    fix = found.commits[0]
    assert fix.files == ["tests/test_textkit.py", "textkit.py"]  # the whole commit, not just
    assert fix.categories == ["source", "tests"]  # the file that was asked for
    mined = await precedents.mine(checkout, rules)
    assert mined.sample_size == 2 and mined.rules == []


# ---- readiness ---------------------------------------------------------------------

LEARNED = Precedents(
    sample_size=40,
    changelog_hint="changes/",
    median_files_changed=2,
    rules=[
        CoChangeRule(when="source", expect="tests", support=30, confidence=0.93),
        CoChangeRule(when="source", expect="changelog", support=30, confidence=0.8),
    ],
)
PASSING = {"test": {"ok": True, "fingerprint": "fp"}, "lint": {"ok": True, "fingerprint": "fp"}}


def playbook(precedent=LEARNED, policy=None, lint="ruff check .") -> Playbook:
    return Playbook(
        repo="o/r",
        base_commit="a" * 40,
        precedents=precedent,
        tooling=Tooling(install_command="i", test_command="t", lint_command=lint, detected_from=[]),
        policy=policy or ContributionPolicy(),
    )


def codes(files, rules, *, checks=PASSING, **kw) -> dict[str, str]:
    report = assess(files, playbook(**kw), rules, last_checks=checks, fingerprint="fp")
    return {f.code: f.severity for f in report.findings}


def test_complete_change_is_ready(rules):
    files = ["src/a.py", "tests/test_a.py", "changes/12.fix.md"]
    report = assess(files, playbook(), rules, last_checks=PASSING, fingerprint="fp")
    assert report.ready and report.findings == []
    assert report.files_by_category == {
        "source": ["src/a.py"],
        "tests": ["tests/test_a.py"],
        "changelog": ["changes/12.fix.md"],
    }


def test_missing_tests_and_changelog_are_flagged_with_evidence(rules):
    report = assess(["src/a.py"], playbook(), rules, last_checks=PASSING, fingerprint="fp")
    assert not report.ready
    by_code = {f.code: f for f in report.findings}
    assert "93%" in by_code["missing-tests"].message
    assert "usually under changes/" in by_code["missing-changelog"].message
    assert by_code["missing-tests"].evidence == "30 commits sampled"


def test_rules_only_apply_when_source_changed(rules):
    assert codes(["docs/guide.md"], rules) == {}


def test_check_state_findings(rules):
    assert codes(["tests/t.py"], rules, checks={}) == {
        "test-not-run": "warning",
        "lint-not-run": "warning",
    }
    stale = {"test": {"ok": True, "fingerprint": "old"}, "lint": {"ok": True, "fingerprint": "fp"}}
    assert codes(["tests/t.py"], rules, checks=stale) == {"test-stale": "warning"}
    failing = {**PASSING, "test": {"ok": False, "fingerprint": "fp"}}
    assert codes(["tests/t.py"], rules, checks=failing) == {"test-failing": "blocker"}
    assert codes(["tests/t.py"], rules, checks={"test": PASSING["test"]}, lint=None) == {}


def test_scope_warnings(rules):
    found = codes(
        ["src/a.py", "tests/t.py", "changes/1.md", "package-lock.json", ".github/workflows/ci.yml"],
        rules,
    )
    assert found == {"lockfile-changed": "warning", "ci-changed": "warning"}
    many = [f"src/m{i}.py" for i in range(8)] + ["tests/t.py", "changes/1.md"]
    assert codes(many, rules)["large-change"] == "warning"  # 10 files vs a median of 2


def test_human_only_requirements_do_not_block(rules):
    policy = ContributionPolicy(
        requires_dco_signoff=True, requires_cla=True, ai_policy_mentions=["Disclose AI use."]
    )
    files = ["src/a.py", "tests/t.py", "changes/1.md"]
    report = assess(files, playbook(policy=policy), rules, last_checks=PASSING, fingerprint="fp")
    assert {f.code: f.severity for f in report.findings} == {
        "dco": "info",
        "cla": "info",
        "ai-policy": "info",
    }
    assert report.ready  # information for the human, not a defect in the diff


def test_empty_diff_is_a_blocker(rules):
    assert codes([], rules) == {"empty-diff": "blocker"}


def test_assess_is_deterministic(rules):
    args = (["src/a.py"], playbook(), rules)
    assert assess(*args, last_checks={}, fingerprint="x") == assess(
        *args, last_checks={}, fingerprint="x"
    )


# ---- through the server ------------------------------------------------------------


@pytest.fixture
def busy_repo(tmp_path):
    """A repo whose history clearly pairs source changes with tests and a changelog entry."""
    repo = tmp_path / "busy"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "changes").mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.co",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.co",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    for i in range(10):
        (repo / "src" / f"m{i}.py").write_text(f"X = {i}\n")
        (repo / "tests" / f"test_m{i}.py").write_text(f"def test_{i}():\n    assert True\n")
        (repo / "changes" / f"{i}.fix.md").write_text("Fixed.\n")
        git("add", "-A")
        git("commit", "-q", "-m", f"fix: change {i}")
    return str(repo)


async def test_check_readiness_tool_end_to_end(services, busy_repo):
    from fastmcp import Client

    from merge_mcp.server import create_server

    async with Client(create_server(services)) as client:
        handle = await client.call_tool("start_workspace", {"repo": busy_repo})
        job = (
            await client.call_tool(
                "get_job", {"job_id": handle.structured_content["job_id"], "wait_seconds": 30}
            )
        ).structured_content
        assert job["state"] == "succeeded", job["error"]
        learned = job["result"]["playbook"]["precedents"]
        assert {r["expect"]: r["confidence"] for r in learned["rules"]} == {
            "tests": 1.0,
            "changelog": 1.0,
        }
        wid = job["result"]["workspace"]["workspace_id"]

        await client.call_tool(
            "write_file", {"workspace_id": wid, "path": "src/m0.py", "content": "X = 99\n"}
        )
        report = (
            await client.call_tool("check_readiness", {"workspace_id": wid})
        ).structured_content
        found = {f["code"] for f in report["findings"]}
        assert {"missing-tests", "missing-changelog", "test-not-run"} <= found
        assert report["ready"] is False

        examples = (
            await client.call_tool("find_precedents", {"workspace_id": wid, "paths": ["src/m0.py"]})
        ).structured_content
        assert examples["commits"][0]["categories"] == ["changelog", "source", "tests"]

        await client.call_tool(
            "write_file",
            {
                "workspace_id": wid,
                "path": "tests/test_m0.py",
                "content": "def test_0():\n    assert 99\n",
            },
        )
        await client.call_tool(
            "write_file",
            {"workspace_id": wid, "path": "changes/99.fix.md", "content": "Changed X.\n"},
        )
        report = (
            await client.call_tool("check_readiness", {"workspace_id": wid})
        ).structured_content
        assert {f["code"] for f in report["findings"]} == {"test-not-run"}
