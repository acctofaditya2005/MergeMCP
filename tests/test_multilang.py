"""The same server code driving real toolchains in three languages.

Each fixture is a tiny repository with a CI workflow. Nothing in these tests, and nothing
in the server, names the language: the commands come from the workflow and the rules file.
Tests are skipped when a toolchain is not installed. They use no third-party dependencies,
so they run offline.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client

from merge_mcp.server import create_server

CI = """\
name: CI
on: [push, pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: {action}
{inputs}      - run: {install}
      - run: {test}
"""

REPOS = {
    "node": {
        "tool": "node",
        "action": "actions/setup-node@v4",
        "inputs": "        with:\n          node-version: 22\n",
        "install": "npm install --no-audit --no-fund",
        "test": "npm test",
        "files": {
            "package.json": '{"name": "calc", "version": "1.0.0", "type": "module", '
            '"scripts": {"test": "node --test"}}\n',
            "calc.js": "export function add(a, b) {\n  return a + b;\n}\n",
            "calc.test.js": "import test from 'node:test';\nimport assert from 'node:assert';\n"
            "import { add } from './calc.js';\n\n"
            "test('add', () => {\n  assert.strictEqual(add(2, 3), 5);\n});\n",
        },
        "source": "calc.js",
        "good": "return a + b;",
        "bad": "return a - b;",
        "image": "node:22",
    },
    "go": {
        "tool": "go",
        "action": "actions/setup-go@v5",
        "inputs": "        with:\n          go-version: '1.23'\n",
        "install": "go mod download",
        "test": "go test ./...",
        "files": {
            "go.mod": "module example.com/calc\n\ngo 1.21\n",
            "calc.go": "package calc\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n",
            "calc_test.go": 'package calc\n\nimport "testing"\n\n'
            "func TestAdd(t *testing.T) {\n\tif Add(2, 3) != 5 {\n"
            '\t\tt.Fatal("wrong sum")\n\t}\n}\n',
        },
        "source": "calc.go",
        "good": "return a + b",
        "bad": "return a - b",
        "image": "golang:1.23",
    },
    "rust": {
        "tool": "cargo",
        "action": "dtolnay/rust-toolchain@stable",
        "inputs": "",
        "install": "cargo build",
        "test": "cargo test",
        "files": {
            "Cargo.toml": '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n',
            "src/lib.rs": "pub fn add(a: i32, b: i32) -> i32 {\n    a + b\n}\n\n"
            "#[cfg(test)]\nmod tests {\n    use super::*;\n\n    #[test]\n"
            "    fn adds() {\n        assert_eq!(add(2, 3), 5);\n    }\n}\n",
        },
        "source": "src/lib.rs",
        "good": "    a + b\n",
        "bad": "    a - b\n",
        "image": "rust:1",
    },
}


def make_repo(root: Path, spec: dict) -> str:
    repo = root / "repo"
    files = {
        **spec["files"],
        ".github/workflows/ci.yml": CI.format(
            **{k: spec[k] for k in ("action", "inputs", "install", "test")}
        ),
    }
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e.co",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e.co",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(root),
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    return str(repo)


async def finish(client: Client, handle) -> dict:
    job_id = handle.structured_content["job_id"]
    while True:
        status = (
            await client.call_tool("get_job", {"job_id": job_id, "wait_seconds": 60})
        ).structured_content
        if status["state"] != "running":
            return status


@pytest.mark.parametrize("language", sorted(REPOS))
async def test_contribution_loop_in_each_language(language, services, tmp_path):
    spec = REPOS[language]
    if shutil.which(spec["tool"]) is None:
        pytest.skip(f"{spec['tool']} is not installed")
    repo = make_repo(tmp_path, spec)

    async with Client(create_server(services)) as client:
        job = await finish(client, await client.call_tool("start_workspace", {"repo": repo}))
        assert job["state"] == "succeeded", job["error"]
        tooling = job["result"]["playbook"]["tooling"]
        wid = job["result"]["workspace"]["workspace_id"]

        # Everything below was learned from the repo's CI file and the rules file.
        assert tooling["ecosystem"] == language
        assert tooling["source"] == "ci: .github/workflows/ci.yml (job: test)"
        assert (tooling["install_command"], tooling["test_command"]) == (
            spec["install"],
            spec["test"],
        )
        assert tooling["image"] == spec["image"]

        setup = await finish(
            client, await client.call_tool("start_check", {"workspace_id": wid, "kind": "setup"})
        )
        assert setup["result"]["ok"], setup["result"]["output_tail"]

        passing = await finish(
            client, await client.call_tool("start_check", {"workspace_id": wid, "kind": "test"})
        )
        assert passing["result"]["ok"], passing["result"]["output_tail"]
        assert passing["result"]["tests"] is None  # judged by exit code: no parser needed

        # Break the code through the file tools: the same check must now fail.
        await client.call_tool(
            "edit_file",
            {
                "workspace_id": wid,
                "path": spec["source"],
                "old_text": spec["good"],
                "new_text": spec["bad"],
            },
        )
        failing = await finish(
            client, await client.call_tool("start_check", {"workspace_id": wid, "kind": "test"})
        )
        assert failing["result"]["ok"] is False

        # Build products and caches the toolchain created must stay out of the diff.
        diff = (await client.call_tool("get_diff", {"workspace_id": wid})).structured_content
        assert diff["files_changed"] == [spec["source"]]

        report = (
            await client.call_tool("check_readiness", {"workspace_id": wid})
        ).structured_content
        assert "test-failing" in {f["code"] for f in report["findings"]}

        await client.call_tool("delete_workspace", {"workspace_id": wid})
