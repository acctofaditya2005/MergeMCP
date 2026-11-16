import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastmcp import Client

from merge_mcp.eval.agent import (
    AGENT_TOOLS,
    LLMAgent,
    NoopAgent,
    OracleAgent,
    tool_specs,
)
from merge_mcp.eval.cli import main as cli_main
from merge_mcp.eval.grade import GradingError, validate_task
from merge_mcp.eval.llm import (
    AnthropicProvider,
    LLMResponse,
    OpenAICompatProvider,
    ScriptedProvider,
    ToolCall,
    ToolResult,
    ToolSpec,
    Turn,
)
from merge_mcp.eval.metrics import Attempt, file_overlap, pass_at_k, render_report, summarize
from merge_mcp.eval.mine import changed_lines, classify, issue_refs, mine_repo
from merge_mcp.eval.run import run_benchmark
from merge_mcp.eval.tasks import is_test_path, load_tasks, save_task, split_patch
from merge_mcp.server import create_server

# ---- metrics ---------------------------------------------------------------------


def test_pass_at_k_matches_closed_form():
    assert pass_at_k(5, 0, 1) == 0.0
    assert pass_at_k(5, 5, 3) == 1.0
    assert pass_at_k(10, 3, 1) == pytest.approx(0.3)
    assert pass_at_k(4, 1, 2) == pytest.approx(0.5)  # 1 - C(3,2)/C(4,2)
    assert pass_at_k(5, 2, 5) == 1.0  # k = n with any success
    with pytest.raises(ValueError):
        pass_at_k(3, 1, 4)


def test_file_overlap():
    assert file_overlap(["a.py", "b.py"], ["a.py"]) == (0.5, 1.0)
    assert file_overlap([], ["a.py"]) == (None, 0.0)


def attempt(task, i, resolved, **kw):
    return Attempt(task_id=task, attempt=i, agent="x", resolved=resolved, **kw)


def test_summarize_and_report():
    attempts = [
        attempt(
            "t1",
            1,
            True,
            latency_s=10,
            input_tokens=1000,
            output_tokens=100,
            cost_usd=0.02,
            tool_calls=4,
            tool_errors=1,
            steps=5,
            files_recall=1.0,
        ),
        attempt(
            "t1",
            2,
            False,
            latency_s=20,
            input_tokens=3000,
            output_tokens=300,
            cost_usd=0.04,
            tool_calls=6,
            steps=7,
            files_recall=0.0,
        ),
        attempt("t2", 1, False, latency_s=30, cost_usd=0.01, error="GradingError: x"),
        attempt("t2", 2, False, latency_s=40, cost_usd=0.01),
    ]
    s = summarize(attempts)
    assert (s.tasks, s.attempts_per_task) == (2, 2)
    assert s.pass_at == {"1": 0.25, "2": 0.5}
    assert s.resolved_rate == 0.25
    assert s.mean_latency_s == 25 and s.p95_latency_s == 40
    assert s.tool_error_rate == 0.1
    assert s.total_cost_usd == 0.08 and s.cost_per_resolved_task_usd == 0.08
    assert s.errors == 1 and s.mean_files_recall == 0.5
    report = render_report(s, attempts)
    assert "| Pass@2 | 50.0% |" in report and "| t1 | 1/2 | pass fail |" in report


# ---- task helpers ----------------------------------------------------------------


def test_is_test_path():
    for p in (
        "tests/test_x.py",
        "pkg/tests/helpers.py",
        "test_x.py",
        "pkg/x_test.py",
        "conftest.py",
        "testing/util.py",
    ):
        assert is_test_path(p), p
    for p in ("pkg/core.py", "pkg/testimonials.py", "contest.py"):
        assert not is_test_path(p), p


def test_split_patch(fixture_repo):
    both = fixture_repo["gold_patch"] + fixture_repo["test_patch"]
    chunks = split_patch(both)
    assert list(chunks) == ["textkit.py", "tests/test_textkit.py"]
    assert "".join(chunks.values()) == both


def test_task_roundtrip_and_validated_filter(task, tmp_path):
    save_task(task, tmp_path)
    assert load_tasks(tmp_path) == []  # unvalidated tasks are never run by default
    assert load_tasks(tmp_path, only_validated=False) == [task]
    assert task.gold_files == ["textkit.py"]


# ---- validation and grading ------------------------------------------------------


async def test_validate_fills_in_test_lists(services, task):
    valid = await validate_task(services, task)
    assert valid.validated
    assert valid.fail_to_pass == ["tests/test_textkit.py::test_slugify_strips_punctuation"]
    assert valid.pass_to_pass == [
        "tests/test_textkit.py::test_slugify_basic",
        "tests/test_textkit.py::test_truncate",
    ]
    assert services.workspaces.list() == []  # the scratch workspace is cleaned up


CLOCK_TEST = """

import time
import pytest


@pytest.mark.parametrize("stamp", [time.time_ns()])
def test_clock(stamp):
    assert stamp
"""


def test_patch_with_clock_test(fixture_repo) -> str:
    """The fixture's reference tests plus one test whose id contains the current time."""
    repo = fixture_repo["path"]
    with open(f"{repo}/tests/test_textkit.py", "a") as fh:
        fh.write(CLOCK_TEST)
    done = subprocess.run(
        ["git", "diff", fixture_repo["base"], "--", "tests"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


test_patch_with_clock_test.__test__ = False  # a helper, not a test


async def test_validate_drops_tests_whose_ids_change_between_runs(services, task, fixture_repo):
    """A test parametrized with the current time gets a new id on every run. It must not be
    recorded, or no later run could ever match it."""
    unstable = task.model_copy(update={"test_patch": test_patch_with_clock_test(fixture_repo)})
    valid = await validate_task(services, unstable)
    assert valid.fail_to_pass == ["tests/test_textkit.py::test_slugify_strips_punctuation"]
    assert not any("test_clock" in t for t in valid.fail_to_pass + valid.pass_to_pass)


async def test_validate_rejects_a_fix_that_fixes_nothing(services, task):
    useless = task.model_copy(update={"gold_patch": ""})
    with pytest.raises(GradingError, match="does not turn any failing"):
        await validate_task(services, useless)


async def test_validate_rejects_a_patch_that_does_not_apply(services, task):
    broken = task.model_copy(update={"gold_patch": task.gold_patch.replace("split()", "nope()")})
    with pytest.raises(GradingError, match="does not apply"):
        await validate_task(services, broken)


@pytest.fixture
async def valid_task(services, task):
    return await validate_task(services, task)


async def test_baselines_bracket_the_benchmark(services, valid_task, tmp_path):
    """Oracle must score 100% and no-op 0%, otherwise the harness itself is wrong."""
    oracle, attempts = await run_benchmark(
        services, [valid_task], OracleAgent, k=2, out_dir=tmp_path / "oracle"
    )
    assert oracle.pass_at == {"1": 1.0, "2": 1.0}
    assert attempts[0].files_recall == 1.0 and attempts[0].tool_calls == 1
    assert {p.name for p in (tmp_path / "oracle").iterdir()} == {
        "attempts.jsonl",
        "summary.json",
        "report.md",
        "artifacts",
    }

    noop, attempts = await run_benchmark(services, [valid_task], NoopAgent, k=2)
    assert noop.pass_at == {"1": 0.0, "2": 0.0}
    assert attempts[0].patch_empty and attempts[0].pass_to_pass_passed == 2
    assert services.workspaces.list() == []


def call(name: str, **arguments) -> ToolCall:
    return ToolCall(
        f"c{abs(hash(json.dumps(arguments, sort_keys=True))) % 10_000}", name, arguments
    )


GOOD_EDIT = call(
    "edit_file",
    path="textkit.py",
    old_text='return "-".join(text.lower().split())',
    new_text='return "-".join("".join(c if c.isalnum() else " " for c in text.lower()).split())',
)


async def test_llm_agent_solves_task_through_mcp_tools(services, valid_task):
    provider = ScriptedProvider(
        [
            LLMResponse("Looking.", [call("search_code", pattern="def slugify")], 500, 20),
            LLMResponse("Fixing.", [GOOD_EDIT], 700, 60),
            LLMResponse("Done.", [call("submit", summary="strip punctuation")], 800, 10),
        ]
    )
    summary, (a,) = await run_benchmark(
        services, [valid_task], lambda: LLMAgent(provider), price_in=3.0, price_out=15.0
    )
    assert a.error is None and a.resolved and summary.pass_at == {"1": 1.0}
    assert (a.steps, a.tool_calls, a.tool_errors, a.stop_reason) == (3, 2, 0, "submitted")
    assert (a.input_tokens, a.output_tokens) == (2000, 90)
    assert a.cost_usd == pytest.approx((2000 * 3 + 90 * 15) / 1e6)
    # The model saw the tool result from its first call before deciding the second.
    tool_turn = provider.seen[1][-1]
    assert tool_turn.role == "tool" and "textkit.py" in tool_turn.results[0].content


async def test_agent_cannot_pass_by_weakening_tests(services, valid_task):
    """Edits to test files are thrown away before grading."""
    sabotage = call(
        "write_file",
        path="tests/test_textkit.py",
        content="def test_slugify_strips_punctuation():\n    assert True\n",
    )
    provider = ScriptedProvider(
        [LLMResponse("", [sabotage]), LLMResponse("", [call("submit", summary="done")])]
    )
    _, (a,) = await run_benchmark(services, [valid_task], lambda: LLMAgent(provider))
    assert a.error is None and not a.resolved and a.fail_to_pass_passed == 0


async def test_agent_tool_errors_are_fed_back_not_fatal(services, valid_task):
    provider = ScriptedProvider(
        [
            LLMResponse(
                "",
                [
                    call("read_file", path="../../etc/passwd"),
                    call("delete_workspace"),
                    call("edit_file", path="textkit.py"),
                ],
            ),
            LLMResponse("", [GOOD_EDIT]),
        ]
    )
    _, (a,) = await run_benchmark(services, [valid_task], lambda: LLMAgent(provider, max_steps=5))
    assert a.resolved and a.tool_errors == 3 and a.stop_reason == "no_tool_call"
    errors = provider.seen[1][-1].results
    assert all(r.is_error for r in errors)
    assert "Unknown tool" in errors[1].content  # lifecycle tools are not exposed to the agent


async def test_agent_stops_at_max_steps(services, valid_task):
    loop = [LLMResponse("", [call("list_files", prefix=str(i))]) for i in range(10)]
    _, (a,) = await run_benchmark(
        services, [valid_task], lambda: LLMAgent(ScriptedProvider(loop), max_steps=3)
    )
    assert (a.steps, a.stop_reason, a.resolved) == (3, "max_steps", False)


async def test_agent_tool_schemas_hide_the_workspace_handle(services):
    async with Client(create_server(services)) as client:
        specs = tool_specs(await client.list_tools())
    assert {s.name for s in specs} == {*AGENT_TOOLS, "submit"}
    for spec in specs:
        assert "workspace_id" not in spec.input_schema.get("properties", {})
        assert "workspace_id" not in spec.input_schema.get("required", [])


# ---- providers -------------------------------------------------------------------

TURNS = [
    Turn("user", "Fix it"),
    Turn("assistant", "Reading.", [ToolCall("c1", "read_file", {"path": "a.py"})]),
    Turn("tool", results=[ToolResult("c1", "contents", is_error=False)]),
]
TOOLS = [ToolSpec("read_file", "Read a file", {"type": "object", "properties": {}})]


async def test_anthropic_provider_wire_format():
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="ok"),
                SimpleNamespace(type="tool_use", id="c2", name="read_file", input={"path": "b.py"}),
            ],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        )

    fake = SimpleNamespace(messages=SimpleNamespace(create=create))
    resp = await AnthropicProvider("some-model", client=fake).complete("sys", TURNS, TOOLS)
    assert resp == LLMResponse("ok", [ToolCall("c2", "read_file", {"path": "b.py"})], 11, 7)
    assert captured["system"] == "sys" and captured["model"] == "some-model"
    assert captured["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert captured["messages"][1]["content"][1] == {
        "type": "tool_use",
        "id": "c1",
        "name": "read_file",
        "input": {"path": "a.py"},
    }
    assert captured["messages"][2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "contents", "is_error": False}
        ],
    }


async def test_openai_compatible_provider_wire_format():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c2",
                                    "type": "function",
                                    "function": {
                                        "name": "read_file",
                                        "arguments": '{"path": "b.py"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            },
        )

    provider = OpenAICompatProvider(
        "local-model", base_url="http://localhost:11434/v1", transport=httpx.MockTransport(handler)
    )
    resp = await provider.complete("sys", TURNS, TOOLS)
    assert resp == LLMResponse("", [ToolCall("c2", "read_file", {"path": "b.py"})], 11, 7)
    assert seen["path"] == "/v1/chat/completions"
    roles = [m["role"] for m in seen["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert seen["body"]["messages"][3] == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": "contents",
    }
    assert seen["body"]["tools"][0]["function"]["name"] == "read_file"


# ---- mining ----------------------------------------------------------------------


def test_issue_refs_prefers_closing_keywords():
    msg = "Handle tabs (#120)\n\nSee #7. Fixes #88 and closes https://github.com/o/r/issues/90"
    assert issue_refs(msg, "o/r") == [88, 90, 120, 7]
    assert issue_refs("no refs here", "o/r") == []


def test_classify_commit_shapes():
    ok = [("M", "pkg/core.py"), ("M", "tests/test_core.py"), ("A", "changelog/88.fix.rst")]
    assert classify(ok, 3) == (["tests/test_core.py"], ["pkg/core.py"])
    assert classify([("M", "pkg/core.py")], 3) is None  # no tests
    assert classify([("M", "tests/test_core.py")], 3) is None  # no source
    assert classify([*ok, ("M", "pyproject.toml")], 3) is None  # config change
    assert classify([*ok, ("D", "pkg/old.py")], 3) is None  # deletion
    assert classify([*ok, ("M", "pkg/a.py"), ("M", "pkg/b.py"), ("M", "pkg/c.py")], 3) is None


def test_changed_lines_ignores_headers(fixture_repo):
    assert changed_lines(fixture_repo["gold_patch"]) == 3


class FakeGitHub:
    async def issue(self, repo, number):
        return {
            "number": number,
            "title": "slugify keeps punctuation",
            "body": "slugify('Hello, World!') should return 'hello-world', not the raw text.",
        }


async def test_mine_then_validate_from_git_history(services, fixture_repo, tmp_path, capsys):
    (mined,) = await mine_repo(FakeGitHub(), "o/textkit", clone_url=fixture_repo["path"])
    assert mined.task_id == "o__textkit-7"
    assert mined.base_commit == fixture_repo["base"]
    assert mined.source_commit == fixture_repo["head"]
    assert mined.test_files == ["tests/test_textkit.py"]
    assert "textkit.py" in mined.gold_patch and "tests/" not in mined.gold_patch
    assert not mined.validated and mined.fail_to_pass == []


def test_cli_report_roundtrip(tmp_path, capsys):
    run = tmp_path / "run"
    run.mkdir()
    (run / "attempts.jsonl").write_text(attempt("t1", 1, True).model_dump_json() + "\n")
    cli_main(["report", str(run)])
    assert "| Pass@1 | 100.0% |" in capsys.readouterr().out


# ---- ablation and convention metrics -----------------------------------------------


async def test_disabled_tools_are_withheld_and_named_in_the_run(services, valid_task):
    """An ablation run must not be able to call the tool it is supposed to lack."""
    provider = ScriptedProvider(
        [
            LLMResponse("", [call("get_playbook"), call("start_check", kind="test")]),
            LLMResponse("", [GOOD_EDIT]),
        ]
    )
    agent = LLMAgent(provider, disabled_tools=("get_playbook", "start_check", "get_job"))
    assert agent.name == "scripted -[get_job,get_playbook,start_check]"

    async with Client(create_server(services)) as client:
        offered = {s.name for s in tool_specs(await client.list_tools(), agent.tools)}
    assert offered.isdisjoint({"get_playbook", "start_check", "get_job"})
    assert {"read_file", "edit_file", "submit"} <= offered

    _, (a,) = await run_benchmark(services, [valid_task], lambda: agent)
    assert a.agent == agent.name
    assert a.resolved and a.tool_errors == 2  # both withheld calls were refused
    assert all("Unknown tool" in r.content for r in provider.seen[1][-1].results)


def test_disabling_an_unknown_tool_is_rejected():
    with pytest.raises(ValueError, match="unknown tools"):
        LLMAgent(ScriptedProvider([]), disabled_tools=("teleport",))


async def test_attempts_record_convention_adherence(services, valid_task):
    """Two patches can both pass the tests; only one of them also brought a test."""
    add_test = call(
        "write_file",
        path="tests/test_extra.py",
        content="from textkit import slugify\n\n\ndef test_extra():\n"
        "    assert slugify('a, b') == 'a-b'\n",
    )
    with_tests = ScriptedProvider([LLMResponse("", [GOOD_EDIT, add_test])])
    without = ScriptedProvider([LLMResponse("", [GOOD_EDIT])])
    _, (a,) = await run_benchmark(services, [valid_task], lambda: LLMAgent(with_tests))
    _, (b,) = await run_benchmark(services, [valid_task], lambda: LLMAgent(without))
    assert a.resolved and b.resolved
    assert (a.tests_included, b.tests_included) == (True, False)
    summary = summarize([a, b])
    assert summary.tests_included_rate == 0.5
    assert summary.mean_tool_calls == 1.5
    assert "| Patches that include tests | 50.0% |" in render_report(summary, [a, b])


# ---- lint grading ------------------------------------------------------------------

UNUSED_IMPORT = call(
    "edit_file",
    path="textkit.py",
    old_text='"""Tiny text helpers."""',
    new_text='"""Tiny text helpers."""\n\nimport os',
)


async def test_validation_records_the_lint_baseline(valid_task):
    assert valid_task.lint_baseline == "clean"


async def test_a_patch_can_pass_the_tests_and_still_fail_the_linter(services, valid_task):
    """Passing tests is not the same as being acceptable: the linter would bounce this."""
    clean = ScriptedProvider([LLMResponse("", [GOOD_EDIT])])
    sloppy = ScriptedProvider([LLMResponse("", [GOOD_EDIT, UNUSED_IMPORT])])
    _, (a,) = await run_benchmark(services, [valid_task], lambda: LLMAgent(clean))
    _, (b,) = await run_benchmark(services, [valid_task], lambda: LLMAgent(sloppy))
    assert (a.resolved, a.lint_clean) == (True, True)
    assert (b.resolved, b.lint_clean) == (True, False)

    summary = summarize([a, b])
    assert summary.resolved_rate == 1.0
    assert (summary.lint_graded_attempts, summary.lint_clean_rate) == (2, 0.5)
    assert summary.resolved_and_lint_clean_rate == 0.5
    assert "| Resolved and lint-clean (2 gradable attempts) | 50.0% |" in render_report(
        summary, [a, b]
    )


@pytest.mark.parametrize(
    ("lint_command", "baseline"),
    [("false", "dirty"), ("no-such-linter-xyz .", "none")],
)
async def test_tasks_without_a_clean_lint_baseline_are_not_lint_graded(
    services, task, lint_command, baseline
):
    """An agent is never blamed for lint problems that were there before it started, and a
    missing linter is 'not gradable', not a failure."""
    valid = await validate_task(services, task.model_copy(update={"lint_command": lint_command}))
    assert valid.lint_baseline == baseline
    provider = ScriptedProvider([LLMResponse("", [GOOD_EDIT])])
    summary, (a,) = await run_benchmark(services, [valid], lambda: LLMAgent(provider))
    assert a.resolved and a.lint_clean is None
    assert summary.lint_graded_attempts == 0 and summary.lint_clean_rate is None
    assert "Lint-clean" not in render_report(summary, [a])


async def test_baseline_is_measured_at_run_time_for_older_task_files(services, valid_task):
    """Task files written before lint grading existed have no recorded baseline."""
    legacy = valid_task.model_copy(update={"lint_baseline": None})
    _, (a,) = await run_benchmark(services, [legacy], OracleAgent)
    assert a.resolved and a.lint_clean is True
