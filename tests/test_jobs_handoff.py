import asyncio
from pathlib import Path

import pytest

from merge_mcp.handoff import build_draft
from merge_mcp.jobs import JobRegistry, UnknownJob
from merge_mcp.models import (
    CoChangeRule,
    CommitConventions,
    ContributionPolicy,
    DiffResult,
    Playbook,
    Precedents,
    Tooling,
    Workspace,
)
from merge_mcp.readiness import assess

# ---- jobs ------------------------------------------------------------------------


async def test_job_runs_in_background_and_long_polls(tmp_path):
    jobs = JobRegistry(tmp_path)
    gate = asyncio.Event()

    async def work():
        await gate.wait()
        return {"answer": 42}

    handle = jobs.start("demo", work)
    assert handle.state == "running"
    assert (await jobs.status(handle.job_id)).state == "running"
    assert (await jobs.status(handle.job_id, wait_s=0.05)).state == "running"
    gate.set()
    done = await jobs.status(handle.job_id, wait_s=5)
    assert (done.state, done.result) == ("succeeded", {"answer": 42})


async def test_failed_job_reports_error_as_data(tmp_path):
    jobs = JobRegistry(tmp_path)

    async def work():
        raise RuntimeError("boom")

    status = await jobs.wait(jobs.start("demo", work).job_id)
    assert status.state == "failed" and status.error == "RuntimeError: boom"


async def test_finished_jobs_survive_a_restart(tmp_path):
    first = JobRegistry(tmp_path)

    async def work():
        return {"ok": True}

    job_id = first.start("demo", work).job_id
    await first.wait(job_id)
    restarted = JobRegistry(tmp_path)  # fresh process: nothing in memory
    assert (await restarted.status(job_id)).result == {"ok": True}


async def test_unknown_job(tmp_path):
    with pytest.raises(UnknownJob):
        await JobRegistry(tmp_path).status("job_doesnotexist")
    with pytest.raises(UnknownJob):
        await JobRegistry(tmp_path).status("../../etc/passwd")


# ---- handoff ---------------------------------------------------------------------


def make(
    tmp_path: Path,
    *,
    policy: ContributionPolicy | None = None,
    style="conventional",
    lint="ruff check .",
    issue=7,
    precedents: Precedents | None = None,
):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    ws = Workspace(
        workspace_id="ws_0123456789",
        repo="o/r",
        source_url="u",
        base_commit="a" * 40,
        branch="fix/issue-7",
        issue_number=issue,
        path=str(repo),
        created_at="now",
    )
    playbook = Playbook(
        repo="o/r",
        base_commit="a" * 40,
        tooling=Tooling(
            install_command="pip install -e .",
            test_command="python -m pytest",
            lint_command=lint,
            detected_from=[],
        ),
        commits=CommitConventions(
            sample_size=10,
            conventional_commits_ratio=0.9,
            scoped_prefix_ratio=0,
            median_subject_length=40,
            signed_off_ratio=0,
            style=style,
            examples=["fix: tabs"],
        ),
        precedents=precedents,
        policy=policy or ContributionPolicy(),
    )
    diff = DiffResult(
        base_commit="a" * 40,
        files_changed=["src/x.py"],
        insertions=1,
        deletions=1,
        diff="d",
        truncated=False,
    )
    return ws, playbook, diff


def draft(
    tmp_path,
    rules,
    ws,
    playbook,
    diff,
    *,
    checks=None,
    fingerprint="f1",
    title="fix: handle tabs",
    notes="",
):
    report = assess(
        diff.files_changed, playbook, rules, last_checks=checks or {}, fingerprint=fingerprint
    )
    return build_draft(
        ws,
        playbook,
        diff,
        report,
        title=title,
        summary="Because.",
        testing_notes=notes,
        workspace_root=tmp_path,
    )


def status_of(result, needle: str) -> str | None:
    return next((c.status for c in result.checklist if needle in c.item), None)


def test_draft_is_never_submitted_and_has_human_commands(tmp_path, rules):
    ws, playbook, diff = make(tmp_path)
    result = draft(
        tmp_path, rules, ws, playbook, diff, title="fix: handle it's edge", notes="Ran pytest."
    )
    assert result.submitted is False
    assert Path(result.body_path).read_text() == result.body
    assert Path(result.body_path).parent == tmp_path  # outside the checkout: cannot be committed
    assert "Fixes #7" in result.body and "AI assistance" in result.body
    joined = "\n".join(result.commands)
    assert "gh pr create --draft" in joined and "git push -u origin fix/issue-7" in joined
    assert """git commit -m 'fix: handle it'"'"'s edge'""" in joined  # shell-quoted
    assert status_of(result, "read the full diff") == "todo"


def test_check_status_tracks_the_exact_diff(tmp_path, rules):
    ws, playbook, diff = make(tmp_path)

    def tests_status(checks, fingerprint="f1"):
        result = draft(tmp_path, rules, ws, playbook, diff, checks=checks, fingerprint=fingerprint)
        return status_of(result, "Test suite")

    ok = {"test": {"ok": True, "fingerprint": "f1", "partial": False}}
    assert tests_status({}) == "todo"
    assert tests_status(ok) == "done"
    assert tests_status(ok, fingerprint="f2") == "warning"  # code changed since
    assert tests_status({"test": {"ok": False, "fingerprint": "f1"}}) == "warning"
    assert tests_status({"test": {"ok": True, "fingerprint": "f1", "partial": True}}) == "warning"
    assert status_of(draft(tmp_path, rules, ws, playbook, diff, checks=ok), "Linter") == "todo"


def test_policy_items_and_signoff(tmp_path, rules):
    policy = ContributionPolicy(
        requires_dco_signoff=True, requires_cla=True, ai_policy_mentions=["Disclose AI use."]
    )
    ws, playbook, diff = make(tmp_path, policy=policy, issue=None)
    result = draft(tmp_path, rules, ws, playbook, diff, title="Handle tabs properly")
    assert any(c.startswith("git commit -s -m") for c in result.commands)
    assert status_of(result, "DCO") == "todo"
    assert status_of(result, "License Agreement") == "todo"
    assert status_of(result, "AI contribution policy") == "todo"
    assert status_of(result, "commit style") == "warning"  # not Conventional Commits
    assert status_of(result, "references an issue") == "warning"
    assert "Fixes #" not in result.body
    assert sum("Signed" in c.item or "signed off" in c.item for c in result.checklist) == 1


def test_precedent_findings_appear_in_the_checklist(tmp_path, rules):
    learned = Precedents(
        sample_size=40,
        changelog_hint="changes/",
        median_files_changed=2,
        rules=[CoChangeRule(when="source", expect="tests", support=30, confidence=0.9)],
    )
    ws, playbook, diff = make(tmp_path, precedents=learned)
    result = draft(tmp_path, rules, ws, playbook, diff)
    assert status_of(result, "No test files changed") == "warning"


def test_repo_pr_template_is_appended(tmp_path, rules):
    policy = ContributionPolicy(pr_template_path=".github/pull_request_template.md")
    ws, playbook, diff = make(tmp_path, policy=policy)
    template = Path(ws.path) / ".github" / "pull_request_template.md"
    template.parent.mkdir()
    template.write_text("- [ ] I added a changelog entry\n")
    result = draft(tmp_path, rules, ws, playbook, diff)
    assert "I added a changelog entry" in result.body
