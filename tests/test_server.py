"""Drive the server the way a real MCP client does: through the protocol."""

import json
import sys

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from merge_mcp.server import create_server

EXPECTED_TOOLS = {
    "find_issues",
    "find_issues_in_dependencies",
    "assess_issue",
    "start_workspace",
    "get_job",
    "list_workspaces",
    "delete_workspace",
    "get_playbook",
    "find_precedents",
    "list_files",
    "read_file",
    "search_code",
    "edit_file",
    "write_file",
    "get_diff",
    "start_check",
    "check_readiness",
    "prepare_pull_request",
    "check_pull_request",
}


@pytest.fixture
async def client(services):
    async with Client(create_server(services)) as c:
        yield c


async def wait(client: Client, handle) -> dict:
    job_id = handle.structured_content["job_id"]
    while True:
        status = (await client.call_tool("get_job", {"job_id": job_id})).structured_content
        if status["state"] != "running":
            return status


async def test_tool_surface_is_stable_and_documented(client):
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == EXPECTED_TOOLS
    for tool in tools.values():
        assert tool.description and len(tool.description) > 30, tool.name
        assert tool.input_schema.get("additionalProperties") is False, tool.name
        assert tool.output_schema, tool.name


async def test_no_tool_can_submit_work(client):
    """The human-in-the-loop gate is structural: these capabilities do not exist."""
    names = " ".join(t.name for t in await client.list_tools())
    for forbidden in (
        "push",
        "commit",
        "fork",
        "merge",
        "create_pull",
        "open_pull",
        "run_command",
        "shell",
        "exec",
    ):
        assert forbidden not in names


async def test_read_only_tools_are_annotated(client):
    tools = {t.name: t for t in await client.list_tools()}
    for name in (
        "find_issues",
        "assess_issue",
        "get_job",
        "get_playbook",
        "read_file",
        "search_code",
        "list_files",
        "get_diff",
        "list_workspaces",
    ):
        assert tools[name].annotations.read_only_hint is True, name
    assert tools["delete_workspace"].annotations.destructive_hint is True
    assert tools["edit_file"].annotations.read_only_hint is False


async def test_full_contribution_flow(client, fixture_repo):
    job = await wait(
        client,
        await client.call_tool(
            "start_workspace",
            {
                "repo": fixture_repo["path"],
                "ref": fixture_repo["base"],
                "install_command": "true",
                "test_command": f"{sys.executable} -m pytest",
            },
        ),
    )
    assert job["state"] == "succeeded", job["error"]
    wid = job["result"]["workspace"]["workspace_id"]
    assert job["result"]["playbook"]["policy"]["requires_dco_signoff"] is True

    # The playbook is also exposed as a resource.
    resource = await client.read_resource(f"mergemcp://workspaces/{wid}/playbook")
    assert json.loads(resource[0].text)["tooling"]["lint_command"] == "ruff check ."

    hits = (
        await client.call_tool("search_code", {"workspace_id": wid, "pattern": "def slugify"})
    ).structured_content
    assert hits["matches"][0]["path"] == "textkit.py"

    await client.call_tool(
        "edit_file",
        {
            "workspace_id": wid,
            "path": "textkit.py",
            "old_text": 'return "-".join(text.lower().split())',
            "new_text": 'return "-".join("".join(c if c.isalnum() else " " '
            "for c in text.lower()).split())",
        },
    )

    assert (
        await wait(
            client, await client.call_tool("start_check", {"workspace_id": wid, "kind": "setup"})
        )
    )["result"]["ok"]
    tests = await wait(
        client, await client.call_tool("start_check", {"workspace_id": wid, "kind": "test"})
    )
    assert tests["result"]["ok"] and len(tests["result"]["tests"]["passed"]) == 2

    draft = (
        await client.call_tool(
            "prepare_pull_request",
            {
                "workspace_id": wid,
                "title": "fix: strip punctuation in slugify",
                "summary": "slugify kept commas and exclamation marks in slugs.",
                "testing_notes": "pytest passes.",
            },
        )
    ).structured_content
    assert draft["submitted"] is False
    assert draft["files_changed"] == ["textkit.py"]
    checklist = {c["item"]: c["status"] for c in draft["checklist"]}
    assert checklist["Test suite passes on this exact diff"] == "done"
    assert checklist["Commit is signed off (DCO)"] == "todo"
    assert any(c.startswith("git commit -s") for c in draft["commands"])

    # Editing after the test run makes the recorded result stale.
    await client.call_tool("write_file", {"workspace_id": wid, "path": "NOTES.md", "content": "x"})
    draft = (
        await client.call_tool(
            "prepare_pull_request",
            {
                "workspace_id": wid,
                "title": "fix: strip punctuation in slugify",
                "summary": "slugify kept commas and exclamation marks in slugs.",
            },
        )
    ).structured_content
    checklist = {c["item"]: c["status"] for c in draft["checklist"]}
    assert checklist["Test suite passes on this exact diff"] == "warning"


async def test_errors_reach_the_model_as_readable_tool_errors(client):
    with pytest.raises(ToolError, match="Unknown workspace"):
        await client.call_tool("read_file", {"workspace_id": "ws_0000000000", "path": "a.py"})
    with pytest.raises(ToolError, match="Provide repo or issue_url"):
        await client.call_tool("start_workspace", {})
    with pytest.raises(ToolError, match="Not a GitHub issue URL"):
        await client.call_tool("assess_issue", {"issue_url": "https://example.com/x"})
    with pytest.raises(ToolError, match="Unknown job"):
        await client.call_tool("get_job", {"job_id": "job_nope"})


async def test_failed_clone_is_reported_through_the_job(client, tmp_path):
    job = await wait(
        client, await client.call_tool("start_workspace", {"repo": str(tmp_path / "missing-repo")})
    )
    assert job["state"] == "failed" and "git fetch failed" in job["error"]


async def test_prepare_pull_request_needs_changes(client, fixture_repo):
    job = await wait(
        client, await client.call_tool("start_workspace", {"repo": fixture_repo["path"]})
    )
    wid = job["result"]["workspace"]["workspace_id"]
    with pytest.raises(ToolError, match="no changes"):
        await client.call_tool(
            "prepare_pull_request",
            {"workspace_id": wid, "title": "fix: nothing", "summary": "Nothing changed here."},
        )


async def test_contribute_prompt(client):
    prompt = await client.get_prompt("contribute", {"issue_url": "https://github.com/o/r/issues/1"})
    text = prompt.messages[0].content.text
    assert "assess_issue" in text and "I will review the diff and submit it myself" in text


async def test_setup_falls_back_to_defaults_when_ci_commands_fail(client, tmp_path):
    """CI does not always reproduce locally. The server retries with the language's own
    defaults and records that it did."""
    import subprocess

    repo = tmp_path / "gorepo"
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/x\n\ngo 1.21\n")
    (repo / "x.go").write_text("package x\n")
    (repo / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - run: make install-tools-that-do-not-exist\n      - run: make test\n"
    )
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.co",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.co",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path),
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)  # noqa: ASYNC221

    job = await wait(client, await client.call_tool("start_workspace", {"repo": str(repo)}))
    wid = job["result"]["workspace"]["workspace_id"]
    assert job["result"]["playbook"]["tooling"]["source"].startswith("ci:")
    assert (
        job["result"]["playbook"]["tooling"]["install_command"]
        == "make install-tools-that-do-not-exist"
    )

    await wait(
        client, await client.call_tool("start_check", {"workspace_id": wid, "kind": "setup"})
    )
    playbook = (await client.call_tool("get_playbook", {"workspace_id": wid})).structured_content
    assert playbook["tooling"]["source"] == "defaults: go"
    assert playbook["tooling"]["test_command"] == "go test ./..."
    assert any("failed; using defaults: go" in note for note in playbook["notes"])


async def test_use_ci_false_skips_the_workflow(client, fixture_repo):
    job = await wait(
        client,
        await client.call_tool("start_workspace", {"repo": fixture_repo["path"], "use_ci": False}),
    )
    assert job["result"]["playbook"]["tooling"]["source"] == "defaults: python"
