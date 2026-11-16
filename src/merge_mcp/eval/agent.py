"""Agents that attempt a task by calling the MCP server's tools.

`LLMAgent` is a deliberately small loop (no framework): ask the model, run the tool calls
it returns through a real MCP client, feed back the results, repeat. Two baselines bracket
it: `NoopAgent` (must score 0%) and `OracleAgent` (writes the maintainers' fix through the
same tools and must score 100%). If either baseline is off, the harness is broken, not
the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastmcp import Client

from ..workspace import git
from .grade import TaskEnv
from .llm import LLMResponse, Provider, ToolCall, ToolResult, ToolSpec, Turn
from .tasks import split_patch

#: The server tools an evaluated agent may call. Discovery and handoff are out of scope
#: for the benchmark, and workspace lifecycle belongs to the harness. An ablation run
#: removes names from this set (`merge-eval run --disable-tools ...`).
AGENT_TOOLS = (
    "get_playbook",
    "find_precedents",
    "list_files",
    "read_file",
    "search_code",
    "edit_file",
    "write_file",
    "get_diff",
    "start_check",
    "get_job",
    "check_readiness",
)
SUBMIT = ToolSpec(
    name="submit",
    description="Call when the fix is complete and the tests you ran pass. Ends the attempt.",
    input_schema={
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    },
)
SYSTEM_PROMPT = """\
You are fixing a GitHub issue in a Python repository. The repository is already cloned and
its dependencies are installed. You work only through the provided tools.

Method:
1. Call get_playbook to learn the project's conventions, then locate the relevant code
   with search_code and read_file.
2. Make the smallest change that fixes the issue. Match the surrounding code style.
3. Verify with whichever of these tools you have: start_check with kind "test" (pass
   test_ids to keep it fast) then get_job until it is no longer running, and
   check_readiness to see what a reviewer would flag.
4. Call submit when done.

Do not edit unrelated code. Hidden tests written by the maintainers decide the outcome,
so changing existing tests to make them pass will not help.
"""
MAX_RESULT_CHARS = 12_000


@dataclass
class Trace:
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    transcript: list[dict[str, Any]] = field(default_factory=list)


class Agent(Protocol):
    name: str

    async def solve(self, env: TaskEnv, client: Client) -> Trace: ...


def tool_specs(mcp_tools: list[Any], allowed: tuple[str, ...] = AGENT_TOOLS) -> list[ToolSpec]:
    """Expose the allowed tools to the model with `workspace_id` removed from each schema.

    The harness injects the id itself, so a model cannot address another workspace.
    """
    specs = []
    for tool in mcp_tools:
        if tool.name not in allowed:
            continue
        schema = json.loads(json.dumps(tool.input_schema))
        schema.get("properties", {}).pop("workspace_id", None)
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r != "workspace_id"]
        specs.append(ToolSpec(tool.name, tool.description or "", schema))
    return [*specs, SUBMIT]


async def call_tool(
    client: Client, workspace_id: str, call: ToolCall, allowed: tuple[str, ...] = AGENT_TOOLS
) -> ToolResult:
    if call.name not in allowed:
        return ToolResult(call.id, f"Unknown tool: {call.name}", is_error=True)
    args = dict(call.arguments)
    if call.name != "get_job":
        args["workspace_id"] = workspace_id
    try:
        result = await client.call_tool(call.name, args, raise_on_error=False)
    except Exception as exc:  # noqa: BLE001 - schema violations etc. go back to the model
        return ToolResult(call.id, f"{type(exc).__name__}: {exc}"[:2000], is_error=True)
    if result.is_error:
        text = " ".join(getattr(c, "text", "") for c in result.content)
        return ToolResult(call.id, text[:2000], is_error=True)
    payload = json.dumps(result.structured_content, ensure_ascii=False)
    if len(payload) > MAX_RESULT_CHARS:
        payload = payload[:MAX_RESULT_CHARS] + " …[truncated]"
    return ToolResult(call.id, payload)


class LLMAgent:
    def __init__(
        self, provider: Provider, *, max_steps: int = 30, disabled_tools: tuple[str, ...] = ()
    ):
        unknown = set(disabled_tools) - set(AGENT_TOOLS)
        if unknown:
            raise ValueError(f"Cannot disable unknown tools: {sorted(unknown)}")
        self.provider = provider
        self.max_steps = max_steps
        self.tools = tuple(t for t in AGENT_TOOLS if t not in disabled_tools)
        # The configuration is part of the name so ablation runs are never confused.
        suffix = f" -[{','.join(sorted(disabled_tools))}]" if disabled_tools else ""
        self.name = provider.name + suffix

    async def solve(self, env: TaskEnv, client: Client) -> Trace:
        trace = Trace()
        tools = tool_specs(await client.list_tools(), self.tools)
        turns = [Turn("user", f"Issue to fix:\n\n{env.task.problem_statement}")]
        for _ in range(self.max_steps):
            response: LLMResponse = await self.provider.complete(SYSTEM_PROMPT, turns, tools)
            trace.steps += 1
            trace.input_tokens += response.input_tokens
            trace.output_tokens += response.output_tokens
            turns.append(Turn("assistant", response.text, response.tool_calls))
            trace.transcript.append(
                {
                    "assistant": response.text,
                    "tool_calls": [
                        {"name": c.name, "arguments": c.arguments} for c in response.tool_calls
                    ],
                }
            )
            if not response.tool_calls:
                trace.stop_reason = "no_tool_call"
                return trace
            results, submitted = [], False
            for call in response.tool_calls:
                if call.name == SUBMIT.name:
                    submitted = True
                    results.append(ToolResult(call.id, "Submitted."))
                    continue
                result = await call_tool(client, env.workspace_id, call, self.tools)
                trace.tool_calls += 1
                trace.tool_errors += result.is_error
                results.append(result)
            turns.append(Turn("tool", results=results))
            trace.transcript.append(
                {
                    "results": [
                        {"is_error": r.is_error, "content": r.content[:2000]} for r in results
                    ]
                }
            )
            if submitted:
                trace.stop_reason = "submitted"
                return trace
        trace.stop_reason = "max_steps"
        return trace


class NoopAgent:
    """Lower bound: changes nothing. Any score above 0% means the tasks are broken."""

    name = "baseline:noop"

    async def solve(self, env: TaskEnv, client: Client) -> Trace:
        return Trace(stop_reason="noop")


class OracleAgent:
    """Upper bound: writes the maintainers' fix through the MCP file tools.

    Any score below 100% means the server tools or the grader are broken.
    """

    name = "baseline:oracle"

    async def solve(self, env: TaskEnv, client: Client) -> Trace:
        trace = Trace(stop_reason="oracle")
        checkout = env.checkout
        paths = list(split_patch(env.task.gold_patch))
        # Compute the post-fix contents, then roll back so the tools do the real writing.
        await env.apply_patch(env.task.gold_patch)
        contents = {
            p: (checkout / p).read_text(encoding="utf-8") for p in paths if (checkout / p).is_file()
        }
        await git(
            "apply", "-R", "--whitespace=nowarn", "-", cwd=checkout, stdin=env.task.gold_patch
        )
        for path in paths:
            if path in contents:
                result = await call_tool(
                    client,
                    env.workspace_id,
                    ToolCall(
                        f"oracle-{trace.tool_calls}",
                        "write_file",
                        {"path": path, "content": contents[path]},
                    ),
                )
                trace.tool_calls += 1
                trace.tool_errors += result.is_error
            else:  # the fix deletes this file; there is no delete tool by design
                (checkout / path).unlink(missing_ok=True)
        return trace
