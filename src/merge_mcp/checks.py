"""Run a repo's own setup, tests and linter inside the sandbox and parse the outcome."""

from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Callable
from pathlib import Path

from .models import CheckKind, CheckResult, Playbook, TestOutcome
from .sandbox import SETUP_MARKER, STATE_DIR, Runner, tail

#: How much raw output a tool result carries back to the model. Per-test outcomes are
#: parsed from the complete output before this trim is applied.
OUTPUT_TAIL_CHARS = 6000
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_RESULT_LINE = re.compile(r"^(PASSED|FAILED|ERROR|XPASS|XFAIL|SKIPPED)\s+(.+)$")


class CheckError(Exception):
    pass


def parse_pytest(output: str) -> TestOutcome:
    """Read the `-rA` short summary pytest prints: one 'STATUS nodeid' line per test."""
    outcome = TestOutcome()
    for raw in _ANSI.sub("", output).splitlines():
        m = _RESULT_LINE.match(raw.strip())
        if not m:
            continue
        status, rest = m.groups()
        # Only non-PASSED lines carry a " - reason" suffix; ids may themselves contain " - ".
        node = rest.strip() if status == "PASSED" else rest.split(" - ", 1)[0].strip()
        if status in ("PASSED", "XFAIL"):
            outcome.passed.append(node)
        elif status in ("FAILED", "XPASS"):
            outcome.failed.append(node)
        elif status == "ERROR":
            outcome.errored.append(node)
        else:
            outcome.skipped += 1
    return outcome


#: Named parsers an ecosystem rule can ask for with `result_parser = "<name>"`. Without
#: one, a check is judged by its exit code alone, which works for every test runner.
PARSERS: dict[str, Callable[[str], TestOutcome]] = {"pytest": parse_pytest}


def build_command(kind: CheckKind, playbook: Playbook, test_ids: list[str] | None = None) -> str:
    tooling = playbook.tooling
    if kind == "setup":
        steps = [
            tooling.env_setup,
            tooling.install_command,
            f"mkdir -p {STATE_DIR} && touch {SETUP_MARKER}",
        ]
        return "\n".join(step for step in steps if step)
    if kind == "test":
        if not tooling.test_command:
            raise CheckError(
                "No test command is known for this repository. Recreate the "
                "workspace with test_command set."
            )
        if test_ids and not tooling.accepts_test_ids:
            raise CheckError(
                f"This repository's test command ({tooling.test_command!r}) does "
                "not take test ids. Run the whole suite by omitting test_ids."
            )
        ids = " ".join(shlex.quote(t) for t in test_ids or [])
        return " ".join(part for part in (tooling.test_command, tooling.test_flags, ids) if part)
    if kind == "lint":
        if not tooling.lint_command:
            raise CheckError("No linter was detected for this repository.")
        return tooling.lint_command
    raise CheckError(f"Unknown check kind: {kind!r}")


async def run_check(
    runner: Runner,
    checkout: Path,
    playbook: Playbook,
    kind: CheckKind,
    *,
    test_ids: list[str] | None = None,
    timeout_s: int,
) -> CheckResult:
    if kind != "setup" and not (checkout / SETUP_MARKER).is_file():
        raise CheckError("The environment is not installed yet. Run start_check(kind='setup').")
    for test_id in test_ids or []:
        if test_id.startswith("-"):
            raise CheckError(f"Test ids cannot be options: {test_id!r}")
    command = build_command(kind, playbook, test_ids)
    tooling = playbook.tooling
    # Network is on only while installing dependencies; tests and linters run offline.
    result = await runner.run(
        checkout,
        command,
        timeout_s=timeout_s,
        network=(kind == "setup"),
        image=tooling.image,
        env=tooling.env,
    )
    parser = PARSERS.get(tooling.result_parser or "") if kind == "test" else None
    return CheckResult(
        kind=kind,
        command=command,
        ok=result.ok,
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_s=result.duration_s,
        sandbox=result.sandbox,
        tests=parser(result.output) if parser else None,
        output_tail=tail(result.output, OUTPUT_TAIL_CHARS),
    )


def diff_fingerprint(diff: str) -> str:
    return hashlib.sha256(diff.encode()).hexdigest()[:16]
