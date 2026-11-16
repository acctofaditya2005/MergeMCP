import shutil
import sys
from pathlib import Path

import pytest

from merge_mcp.checks import CheckError, build_command, parse_pytest, run_check
from merge_mcp.config import Settings
from merge_mcp.sandbox import (
    DockerRunner,
    LocalRunner,
    SandboxUnavailable,
    make_runner,
    tail,
    wrap_command,
)

# ---- sandbox ---------------------------------------------------------------------


def test_docker_argv_is_locked_down(tmp_path):
    runner = DockerRunner(Settings(sandbox_image="python:3.12", sandbox_memory="1g"))
    argv = runner.build_argv(tmp_path, "pytest", network=False, name="n1")
    joined = " ".join(argv)
    assert "--network none" in joined
    assert "--cap-drop ALL" in joined
    assert "--security-opt no-new-privileges" in joined
    assert "--pids-limit 512" in joined and "--memory 1g" in joined
    assert f"--volume {tmp_path}:/work" in joined
    assert joined.count("--volume") == 1  # only the checkout is mounted, nothing from $HOME
    assert "--privileged" not in joined and "docker.sock" not in joined
    assert argv[-3:-1] == ["bash", "-c"] and argv[-1] == "set -e; pytest"


def test_docker_network_only_when_requested(tmp_path):
    runner = DockerRunner(Settings())
    assert "bridge" in runner.build_argv(tmp_path, "pip install x", network=True, name="n")
    assert "bridge" not in runner.build_argv(tmp_path, "pytest", network=False, name="n")


def test_runner_selection():
    assert isinstance(make_runner(Settings(sandbox="docker")), DockerRunner)
    assert isinstance(make_runner(Settings(sandbox="local")), LocalRunner)


def test_settings_reject_unknown_sandbox(monkeypatch):
    monkeypatch.setenv("MERGE_MCP_SANDBOX", "yolo")
    with pytest.raises(ValueError):
        Settings.from_env()


async def test_local_runner_does_not_leak_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    work = tmp_path / "repo"
    work.mkdir()
    result = await LocalRunner(Settings()).run(work, "env", timeout_s=20)
    assert result.ok
    assert "ghp_secret" not in result.output and "sk-secret" not in result.output
    assert f"HOME={tmp_path}/home" in result.output


def test_only_locale_proxy_and_certificate_settings_are_inherited():
    from merge_mcp.sandbox import inheritable

    for name in (
        "PATH",
        "LC_ALL",
        "HTTPS_PROXY",
        "https_proxy",
        "SSL_CERT_FILE",
        "NODE_EXTRA_CA_CERTS",
        "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    ):
        assert inheritable(name), name
    for name in (
        "GITHUB_TOKEN",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HOME",
        "USER",
        "NPM_TOKEN",
        "PROXY_AUTH_PROXY",
        "SSH_AUTH_SOCK",
        "DATABASE_URL",
    ):
        assert not inheritable(name), name


async def test_timeout_keeps_the_output_printed_so_far(tmp_path):
    """The last lines before a hang are usually the only clue to why it hung."""
    work = tmp_path / "repo"
    work.mkdir()
    result = await LocalRunner(Settings()).run(work, "echo fetching deps; sleep 30", timeout_s=1)
    assert result.timed_out and "fetching deps" in result.output
    assert "[timed out after 1s]" in result.output


async def test_local_runner_kills_on_timeout(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    result = await LocalRunner(Settings()).run(work, "sleep 30", timeout_s=1)
    assert result.timed_out and not result.ok and result.duration_s < 10


async def test_local_runner_reports_exit_code(tmp_path):
    work = tmp_path / "repo"
    work.mkdir()
    result = await LocalRunner(Settings()).run(work, "echo out; echo err >&2; exit 3", timeout_s=20)
    assert result.exit_code == 3 and "out" in result.output and "err" in result.output


def test_tail_keeps_the_end():
    assert tail("abc", 10) == "abc"
    assert tail("x" * 50 + "END", 10).endswith("END")
    assert "truncated" in tail("x" * 50, 10)


def test_wrap_command_exports_the_ecosystem_environment(rules):
    wrapped = wrap_command("pytest", rules.ecosystems["python"].env)
    assert wrapped.startswith("set -e; export ")
    assert 'PATH="$PWD/.merge-mcp-venv/bin:$PATH"' in wrapped  # double quotes: $PWD expands
    assert wrapped.endswith("; pytest")
    assert wrap_command("go test ./...") == "set -e; go test ./..."


async def test_hash_seed_is_fixed_so_test_ids_are_stable(tmp_path, rules):
    """Suites that parametrize from a set get different test ids per run otherwise."""
    work = tmp_path / "repo"
    work.mkdir()
    cmd = """python3 -c "print(list({'alpha', 'beta', 'gamma', 'delta'}))" """
    env = rules.ecosystems["python"].env
    runner = LocalRunner(Settings())
    runs = {(await runner.run(work, cmd, timeout_s=20, env=env)).output for _ in range(4)}
    assert len(runs) == 1


async def test_a_failing_line_stops_a_multi_line_script(tmp_path):
    """CI-derived commands are multi-line; a failure must not be masked by a later line."""
    work = tmp_path / "repo"
    work.mkdir()
    result = await LocalRunner(Settings()).run(work, "false\necho reached", timeout_s=20)
    assert not result.ok and "reached" not in result.output


def test_docker_uses_the_playbook_image(tmp_path):
    runner = DockerRunner(Settings(sandbox_image="python:3.12"))
    argv = runner.build_argv(
        tmp_path,
        "go test ./...",
        network=False,
        name="n",
        image="golang:1.23",
        env={"GOFLAGS": "-buildvcs=false"},
    )
    assert "golang:1.23" in argv and "python:3.12" not in argv
    assert 'GOFLAGS="-buildvcs=false"' in argv[-1]


async def test_docker_runner_refuses_to_run_without_a_daemon(tmp_path, monkeypatch):
    """A dead daemon must be an explicit error, never a silent "tests failed"."""
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho 'failed to connect to the docker API' >&2\nexit 1\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:/usr/bin:/bin")
    with pytest.raises(SandboxUnavailable, match="not usable"):
        await DockerRunner(Settings()).run(tmp_path, "pytest", timeout_s=10)


@pytest.mark.docker
async def test_docker_runner_blocks_network(tmp_path):
    if shutil.which("docker") is None:
        pytest.skip("docker not installed")
    runner = DockerRunner(Settings(sandbox_image="python:3.12-slim"))
    code = "import urllib.request as u; u.urlopen('https://pypi.org', timeout=5)"
    try:
        result = await runner.run(tmp_path, f'python -c "{code}"', timeout_s=120)
    except SandboxUnavailable as exc:  # daemon not running on this machine
        pytest.skip(str(exc))
    assert "URLError" in result.output or "Temporary failure" in result.output
    assert not result.ok


# ---- pytest output parsing -------------------------------------------------------

PYTEST_OUTPUT = """\
tests/test_a.py ..F                                                       [100%]
=========================== short test summary info ============================
PASSED tests/test_a.py::test_one
PASSED tests/test_a.py::test_param[a - b]
\x1b[31mFAILED\x1b[0m tests/test_a.py::test_two - AssertionError: assert 1 == 2
ERROR tests/test_b.py - ImportError: cannot import name 'x'
SKIPPED [1] tests/test_a.py:9: needs network
XFAIL tests/test_a.py::test_known
========================= 1 failed, 2 passed in 0.10s ==========================
"""


def test_parse_pytest_summary():
    out = parse_pytest(PYTEST_OUTPUT)
    assert out.passed == [
        "tests/test_a.py::test_one",
        "tests/test_a.py::test_param[a - b]",
        "tests/test_a.py::test_known",
    ]
    assert out.failed == ["tests/test_a.py::test_two"]
    assert out.errored == ["tests/test_b.py"]
    assert out.skipped == 1


# ---- checks on the fixture repo --------------------------------------------------


@pytest.fixture
async def prepared(services, fixture_repo):
    ws = await services.workspaces.create(fixture_repo["path"], ref=fixture_repo["head"])
    checkout = services.workspaces.checkout(ws.workspace_id)
    playbook = await services.build_playbook(
        ws, install_command="true", test_command=f"{sys.executable} -m pytest"
    )
    return checkout, playbook


async def test_tests_require_setup_first(services, prepared):
    checkout, playbook = prepared
    with pytest.raises(CheckError, match="not installed yet"):
        await run_check(services.runner, checkout, playbook, "test", timeout_s=60)


async def test_setup_then_test_reports_per_test_outcomes(services, prepared):
    checkout, playbook = prepared
    setup = await run_check(services.runner, checkout, playbook, "setup", timeout_s=120)
    assert setup.ok and (Path(checkout) / ".merge-mcp-venv").is_dir()
    assert (Path(checkout) / ".merge-mcp-cache" / "setup-ok").is_file()
    result = await run_check(
        services.runner,
        checkout,
        playbook,
        "test",
        test_ids=["tests/test_textkit.py"],
        timeout_s=120,
    )
    assert result.ok and result.sandbox == "local-unsandboxed"
    assert len(result.tests.passed) == 3 and result.tests.failed == []


async def test_long_output_does_not_lose_test_results(services, prepared):
    """Regression: outcomes were parsed after truncation, so verbose suites lost tests."""
    checkout, playbook = prepared
    await run_check(services.runner, checkout, playbook, "setup", timeout_s=120)
    (checkout / "tests" / "test_noisy.py").write_text(
        "import pytest\n\n\n"
        "@pytest.mark.parametrize('i', range(300))\n"
        "def test_noisy(i):\n"
        "    print('x' * 400)\n"
    )
    result = await run_check(
        services.runner, checkout, playbook, "test", test_ids=["tests/test_noisy.py"], timeout_s=120
    )
    assert len(result.tests.passed) == 300
    assert len(result.output_tail) <= 6100  # what goes back to the model stays small


async def test_test_ids_cannot_inject_options(services, prepared):
    checkout, playbook = prepared
    await run_check(services.runner, checkout, playbook, "setup", timeout_s=120)
    with pytest.raises(CheckError, match="cannot be options"):
        await run_check(
            services.runner, checkout, playbook, "test", test_ids=["--rootdir=/"], timeout_s=60
        )


async def test_test_ids_are_shell_quoted(prepared):
    _, playbook = prepared
    cmd = build_command("test", playbook, ["tests/x.py::t[a; rm -rf ~]"])
    assert "'tests/x.py::t[a; rm -rf ~]'" in cmd


# ---- command building for runners the server has no parser for ---------------------


def _playbook(rules, **tooling):
    from merge_mcp.models import ContributionPolicy, Playbook, Tooling

    base = {
        "install_command": "go mod download",
        "test_command": "go test ./...",
        "detected_from": [],
    }
    return Playbook(
        repo="o/r",
        base_commit="a" * 40,
        tooling=Tooling(**{**base, **tooling}),
        policy=ContributionPolicy(),
    )


def test_commands_for_a_runner_without_test_id_support(rules):
    playbook = _playbook(rules)
    assert build_command("test", playbook) == "go test ./..."  # no pytest flags leak in
    with pytest.raises(CheckError, match="does not take test ids"):
        build_command("test", playbook, ["pkg/x_test.go"])
    setup = build_command("setup", playbook).splitlines()
    assert setup == [
        "go mod download",
        "mkdir -p .merge-mcp-cache && touch .merge-mcp-cache/setup-ok",
    ]


def test_missing_test_command_is_a_clear_error(rules):
    with pytest.raises(CheckError, match="No test command is known"):
        build_command("test", _playbook(rules, test_command=""))
