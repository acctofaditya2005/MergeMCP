"""`merge-eval`: mine, validate, run and report."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
from pathlib import Path

from ..config import Settings
from ..github import GitHubClient
from ..server import build_services
from .agent import AGENT_TOOLS, Agent, LLMAgent, NoopAgent, OracleAgent
from .grade import GradingError, validate_task
from .metrics import Attempt, render_report, summarize
from .mine import mine_repo
from .run import run_benchmark
from .tasks import load_tasks, save_task


def _settings(args: argparse.Namespace) -> Settings:
    base = Settings.from_env()
    return dataclasses.replace(
        base,
        home=Path(args.home).resolve(),
        allow_local_sources=True,
        sandbox=args.sandbox or base.sandbox,
    )


def _make_agent(args: argparse.Namespace):
    if args.agent == "noop":
        return NoopAgent
    if args.agent == "oracle":
        return OracleAgent
    if not args.model:
        sys.exit("--model is required for LLM agents.")
    if args.agent == "anthropic":
        from .llm import AnthropicProvider

        provider = AnthropicProvider(args.model, temperature=args.temperature)
    else:
        from .llm import OpenAICompatProvider

        provider = OpenAICompatProvider(
            args.model, base_url=args.base_url, temperature=args.temperature
        )

    disabled = tuple(t.strip() for t in (args.disable_tools or "").split(",") if t.strip())

    def factory() -> Agent:
        return LLMAgent(provider, max_steps=args.max_steps, disabled_tools=disabled)

    return factory


async def _mine(args: argparse.Namespace) -> None:
    settings = _settings(args)
    gh = GitHubClient(settings)
    try:
        tasks = await mine_repo(gh, args.repo, limit=args.limit, max_commits=args.max_commits)
    finally:
        await gh.aclose()
    for task in tasks:
        print("candidate:", save_task(task, Path(args.out)))
    print(f"{len(tasks)} candidate(s). Run `merge-eval validate {args.out}` next.")


async def _validate(args: argparse.Namespace) -> None:
    services = build_services(_settings(args))
    kept = 0
    for task in load_tasks(Path(args.path), only_validated=False):
        try:
            valid = await validate_task(services, task, repeats=args.repeats)
        except GradingError as exc:
            print(f"REJECT {task.task_id}: {str(exc).splitlines()[0][:160]}")
            continue
        save_task(valid, Path(args.out))
        kept += 1
        print(
            f"OK     {task.task_id}: {len(valid.fail_to_pass)} fail-to-pass, "
            f"{len(valid.pass_to_pass)} pass-to-pass"
        )
    print(f"{kept} task(s) validated into {args.out}")
    await services.aclose()


async def _run(args: argparse.Namespace) -> None:
    services = build_services(_settings(args))
    tasks = load_tasks(Path(args.path))
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        sys.exit("No validated tasks found.")

    def progress(a: Attempt) -> None:
        mark = "ERROR" if a.error else "pass" if a.resolved else "fail"
        print(
            f"[{mark}] {a.task_id} #{a.attempt} {a.latency_s:.1f}s "
            f"{a.input_tokens + a.output_tokens} tok" + (f" :: {a.error[:120]}" if a.error else "")
        )

    out = Path(args.out)
    summary, attempts = await run_benchmark(
        services,
        tasks,
        _make_agent(args),
        k=args.k,
        concurrency=args.concurrency,
        out_dir=out,
        price_in=args.price_in,
        price_out=args.price_out,
        keep_workspaces=args.keep,
        on_attempt=progress,
    )
    print()
    print(render_report(summary, attempts))
    print(f"Wrote {out}/report.md, summary.json, attempts.jsonl")
    await services.aclose()


def _report(args: argparse.Namespace) -> None:
    path = Path(args.run) / "attempts.jsonl"
    attempts = [Attempt.model_validate(json.loads(line)) for line in path.read_text().splitlines()]
    print(render_report(summarize(attempts), attempts))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="merge-eval", description=__doc__)
    parser.add_argument("--home", default=".merge-eval", help="Scratch directory.")
    parser.add_argument(
        "--sandbox",
        choices=["docker", "local"],
        help="Overrides MERGE_MCP_SANDBOX. 'local' gives no isolation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("mine", help="Find candidate tasks in a repo's history.")
    p.add_argument("repo", help="owner/name")
    p.add_argument("--out", default="benchmarks/candidates")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--max-commits", type=int, default=400)

    p = sub.add_parser("validate", help="Execute candidates and keep the ones that hold up.")
    p.add_argument("path")
    p.add_argument("--out", default="benchmarks/tasks")
    p.add_argument("--repeats", type=int, default=2)

    p = sub.add_parser("run", help="Run an agent over validated tasks.")
    p.add_argument("path", nargs="?", default="benchmarks/tasks")
    p.add_argument("--agent", choices=["oracle", "noop", "anthropic", "openai"], required=True)
    p.add_argument("--model")
    p.add_argument("--base-url", help="For --agent openai: any OpenAI-compatible endpoint.")
    p.add_argument("--temperature", type=float)
    p.add_argument("--k", type=int, default=1, help="Attempts per task.")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument(
        "--disable-tools",
        help="Comma-separated server tools to withhold from the agent, for ablation runs. "
        f"Choices: {', '.join(AGENT_TOOLS)}",
    )
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--limit", type=int, help="Only the first N tasks.")
    p.add_argument("--price-in", type=float, help="USD per million input tokens.")
    p.add_argument("--price-out", type=float, help="USD per million output tokens.")
    p.add_argument("--keep", action="store_true", help="Keep workspaces for debugging.")
    p.add_argument("--out", default="runs/latest")

    p = sub.add_parser("report", help="Re-render the report for a finished run.")
    p.add_argument("run")

    args = parser.parse_args(argv)
    if args.command == "report":
        _report(args)
    else:
        asyncio.run({"mine": _mine, "validate": _validate, "run": _run}[args.command](args))


if __name__ == "__main__":
    main()
