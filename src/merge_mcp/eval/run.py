"""Run agents over tasks and record one `Attempt` per (task, attempt index)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

from fastmcp import Client

from ..server import Services, create_server
from .agent import Agent
from .grade import GradingError, TaskEnv, grade_attempt
from .metrics import Attempt, Summary, file_overlap, render_report, summarize
from .tasks import Task


async def run_attempt(
    services: Services,
    client: Client,
    task: Task,
    agent: Agent,
    index: int,
    *,
    price_in: float | None = None,
    price_out: float | None = None,
    artifacts: Path | None = None,
    keep_workspace: bool = False,
) -> Attempt:
    record = Attempt(
        task_id=task.task_id,
        attempt=index,
        agent=agent.name,
        resolved=False,
        fail_to_pass_total=len(task.fail_to_pass),
        pass_to_pass_total=len(task.pass_to_pass),
    )
    env: TaskEnv | None = None
    try:
        env = await TaskEnv.create(services, task)
        record.setup_s = env.setup_s
        start = time.monotonic()
        try:
            trace = await agent.solve(env, client)
        finally:
            record.latency_s = round(time.monotonic() - start, 2)
        record.steps, record.tool_calls = trace.steps, trace.tool_calls
        record.tool_errors, record.stop_reason = trace.tool_errors, trace.stop_reason
        record.input_tokens, record.output_tokens = trace.input_tokens, trace.output_tokens
        if price_in is not None and price_out is not None:
            record.cost_usd = round(
                (trace.input_tokens * price_in + trace.output_tokens * price_out) / 1e6, 6
            )

        # Convention adherence is measured on the agent's own diff, before the reference
        # tests replace its test files. Check-status findings are excluded: they describe
        # what the agent ran, not what the patch contains.
        report, _ = await services.readiness(env.workspace_id)
        record.review_warnings = sum(
            f.severity == "warning" and not f.code.startswith(("test-", "lint-"))
            for f in report.findings
        )
        record.tests_included = "tests" in report.files_by_category
        # Lint is graded on the agent's diff, and only where the repo was lint-clean
        # before the agent touched it.
        if env.lint_baseline == "clean":
            record.lint_clean = await env.run_lint()

        grade = await grade_attempt(env)
        record.resolved = grade.resolved
        record.fail_to_pass_passed = grade.fail_to_pass_passed
        record.pass_to_pass_passed = grade.pass_to_pass_passed
        record.patch_empty = not grade.model_files
        record.files_precision, record.files_recall = file_overlap(
            grade.model_files, task.gold_files
        )
        if artifacts is not None:
            out = artifacts / task.task_id / f"attempt-{index}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "model.patch").write_text(grade.model_patch)
            (out / "transcript.json").write_text(json.dumps(trace.transcript, indent=2))
    except GradingError as exc:
        record.error = f"GradingError: {exc}"[:2000]
    except Exception as exc:  # noqa: BLE001 - one bad attempt must not sink the whole run
        record.error = f"{type(exc).__name__}: {exc}"[:2000]
    finally:
        if env is not None and not keep_workspace:
            env.close()
    return record


async def run_benchmark(
    services: Services,
    tasks: list[Task],
    make_agent: Callable[[], Agent],
    *,
    k: int = 1,
    concurrency: int = 2,
    out_dir: Path | None = None,
    price_in: float | None = None,
    price_out: float | None = None,
    keep_workspaces: bool = False,
    on_attempt: Callable[[Attempt], None] | None = None,
) -> tuple[Summary, list[Attempt]]:
    gate = asyncio.Semaphore(max(1, concurrency))
    server = create_server(services)
    artifacts = out_dir / "artifacts" if out_dir else None
    log = None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        log = (out_dir / "attempts.jsonl").open("w")

    async with Client(server) as client:

        async def one(task: Task, index: int) -> Attempt:
            async with gate:
                attempt = await run_attempt(
                    services,
                    client,
                    task,
                    make_agent(),
                    index,
                    price_in=price_in,
                    price_out=price_out,
                    artifacts=artifacts,
                    keep_workspace=keep_workspaces,
                )
            if log:
                log.write(attempt.model_dump_json() + "\n")
                log.flush()
            if on_attempt:
                on_attempt(attempt)
            return attempt

        attempts = list(
            await asyncio.gather(*(one(task, i) for task in tasks for i in range(1, k + 1)))
        )

    if log:
        log.close()
    summary = summarize(attempts)
    if out_dir:
        (out_dir / "summary.json").write_text(summary.model_dump_json(indent=2) + "\n")
        (out_dir / "report.md").write_text(render_report(summary, attempts))
    return summary, attempts
