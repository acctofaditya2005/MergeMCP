"""Benchmark metrics. Pure functions over attempt records."""

from __future__ import annotations

import statistics
from collections import defaultdict
from math import comb

from pydantic import BaseModel


class Attempt(BaseModel):
    task_id: str
    attempt: int
    agent: str
    resolved: bool
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_total: int = 0
    files_precision: float | None = None
    files_recall: float | None = None
    patch_empty: bool = True
    #: Convention adherence, from the server's own readiness rules applied to the patch.
    review_warnings: int | None = None
    tests_included: bool | None = None
    #: None when the task is not lint-gradable (no linter, or the base commit was dirty).
    lint_clean: bool | None = None
    steps: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_s: float = 0.0
    setup_s: float = 0.0
    stop_reason: str = ""
    error: str | None = None


def pass_at_k(n: int, c: int, k: int) -> float:
    """Chance that at least one of k attempts drawn from n (c of them correct) passes.

    The unbiased estimator from the Codex paper: 1 - C(n-c, k) / C(n, k).
    """
    if k < 1 or k > n:
        raise ValueError(f"k must be between 1 and n={n}, got {k}")
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def file_overlap(model_files: list[str], gold_files: list[str]) -> tuple[float | None, float]:
    """Did the agent edit the files the maintainers edited? Returns (precision, recall)."""
    model, gold = set(model_files), set(gold_files)
    hit = len(model & gold)
    precision = hit / len(model) if model else None
    recall = hit / len(gold) if gold else 0.0
    return precision, recall


class Summary(BaseModel):
    agent: str
    tasks: int
    attempts_per_task: int
    pass_at: dict[str, float]
    resolved_rate: float
    mean_latency_s: float
    p95_latency_s: float
    mean_input_tokens: float
    mean_output_tokens: float
    mean_steps: float
    mean_tool_calls: float = 0.0
    tool_error_rate: float
    mean_files_recall: float | None
    mean_review_warnings: float | None = None
    tests_included_rate: float | None = None
    lint_graded_attempts: int = 0
    lint_clean_rate: float | None = None
    resolved_and_lint_clean_rate: float | None = None
    total_cost_usd: float | None
    cost_per_resolved_task_usd: float | None
    errors: int


def summarize(attempts: list[Attempt]) -> Summary:
    if not attempts:
        raise ValueError("No attempts to summarize.")
    by_task: dict[str, list[Attempt]] = defaultdict(list)
    for a in attempts:
        by_task[a.task_id].append(a)
    n = min(len(v) for v in by_task.values())
    pass_at = {
        str(k): round(
            statistics.mean(
                pass_at_k(len(v), sum(a.resolved for a in v), k) for v in by_task.values()
            ),
            4,
        )
        for k in range(1, n + 1)
    }
    latencies = sorted(a.latency_s for a in attempts)
    p95 = latencies[min(len(latencies) - 1, int(round(0.95 * (len(latencies) - 1))))]
    costs = [a.cost_usd for a in attempts if a.cost_usd is not None]
    total_cost = round(sum(costs), 4) if costs else None
    solved_tasks = sum(any(a.resolved for a in v) for v in by_task.values())
    calls = sum(a.tool_calls for a in attempts)
    recalls = [a.files_recall for a in attempts if a.files_recall is not None]
    warnings = [a.review_warnings for a in attempts if a.review_warnings is not None]
    with_tests = [a.tests_included for a in attempts if a.tests_included is not None]
    linted = [a for a in attempts if a.lint_clean is not None]
    return Summary(
        agent=attempts[0].agent,
        tasks=len(by_task),
        attempts_per_task=n,
        pass_at=pass_at,
        resolved_rate=round(sum(a.resolved for a in attempts) / len(attempts), 4),
        mean_latency_s=round(statistics.mean(latencies), 2),
        p95_latency_s=round(p95, 2),
        mean_input_tokens=round(statistics.mean(a.input_tokens for a in attempts), 1),
        mean_output_tokens=round(statistics.mean(a.output_tokens for a in attempts), 1),
        mean_steps=round(statistics.mean(a.steps for a in attempts), 2),
        mean_tool_calls=round(statistics.mean(a.tool_calls for a in attempts), 2),
        tool_error_rate=round(sum(a.tool_errors for a in attempts) / calls, 4) if calls else 0.0,
        mean_files_recall=round(statistics.mean(recalls), 4) if recalls else None,
        mean_review_warnings=round(statistics.mean(warnings), 2) if warnings else None,
        tests_included_rate=round(sum(with_tests) / len(with_tests), 4) if with_tests else None,
        lint_graded_attempts=len(linted),
        lint_clean_rate=round(sum(a.lint_clean for a in linted) / len(linted), 4)
        if linted
        else None,
        resolved_and_lint_clean_rate=round(
            sum(a.resolved and a.lint_clean for a in linted) / len(linted), 4
        )
        if linted
        else None,
        total_cost_usd=total_cost,
        cost_per_resolved_task_usd=(
            round(total_cost / solved_tasks, 4) if total_cost is not None and solved_tasks else None
        ),
        errors=sum(a.error is not None for a in attempts),
    )


def render_report(summary: Summary, attempts: list[Attempt]) -> str:
    s = summary
    lines = [
        f"# Benchmark report: `{s.agent}`",
        "",
        f"{s.tasks} tasks, {s.attempts_per_task} attempt(s) each.",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    lines += [f"| Pass@{k} | {v:.1%} |" for k, v in s.pass_at.items()]
    lines += [
        f"| Resolved rate (all attempts) | {s.resolved_rate:.1%} |",
        f"| Mean latency | {s.mean_latency_s:.1f} s |",
        f"| p95 latency | {s.p95_latency_s:.1f} s |",
        f"| Mean tokens (in / out) | {s.mean_input_tokens:,.0f} / {s.mean_output_tokens:,.0f} |",
        f"| Mean steps | {s.mean_steps:.1f} |",
        f"| Mean tool calls | {s.mean_tool_calls:.1f} |",
        f"| Tool error rate | {s.tool_error_rate:.1%} |",
    ]
    if s.mean_files_recall is not None:
        lines.append(f"| Mean file recall vs maintainers' fix | {s.mean_files_recall:.1%} |")
    if s.mean_review_warnings is not None:
        lines.append(f"| Mean review warnings per patch | {s.mean_review_warnings:.2f} |")
    if s.tests_included_rate is not None:
        lines.append(f"| Patches that include tests | {s.tests_included_rate:.1%} |")
    if s.lint_clean_rate is not None and s.resolved_and_lint_clean_rate is not None:
        n = s.lint_graded_attempts
        lines.append(f"| Lint-clean patches ({n} gradable attempts) | {s.lint_clean_rate:.1%} |")
        lines.append(
            f"| Resolved and lint-clean ({n} gradable attempts) | "
            f"{s.resolved_and_lint_clean_rate:.1%} |"
        )
    if s.total_cost_usd is not None:
        lines.append(f"| Total cost | ${s.total_cost_usd:.2f} |")
    if s.cost_per_resolved_task_usd is not None:
        lines.append(f"| Cost per resolved task | ${s.cost_per_resolved_task_usd:.2f} |")
    lines += [
        f"| Harness errors | {s.errors} |",
        "",
        "## Per task",
        "",
        "| Task | Resolved | Attempts |",
        "|---|---|---|",
    ]
    by_task: dict[str, list[Attempt]] = defaultdict(list)
    for a in attempts:
        by_task[a.task_id].append(a)
    for task_id in sorted(by_task):
        rows = by_task[task_id]
        marks = " ".join("pass" if a.resolved else "fail" for a in rows)
        lines.append(f"| {task_id} | {sum(a.resolved for a in rows)}/{len(rows)} | {marks} |")
    return "\n".join(lines) + "\n"
