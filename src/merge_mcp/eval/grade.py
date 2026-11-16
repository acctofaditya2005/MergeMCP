"""Task environments, task validation and attempt grading.

Grading is outcome-based: an attempt counts as resolved only if the maintainers' tests
pass against the agent's code. The agent's own edits to test files are discarded first,
so an agent cannot pass by weakening a test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from ..checks import run_check
from ..models import Playbook, TestOutcome
from ..server import Services
from ..workspace import WorkspaceError, git
from .tasks import Task, is_test_path, split_patch

REPEAT_GAP_S = 1.5


class GradingError(Exception):
    """The harness could not produce a verdict (not the agent's fault)."""


@dataclass
class TaskEnv:
    """One fresh workspace prepared for one task attempt."""

    services: Services
    task: Task
    workspace_id: str
    playbook: Playbook
    setup_s: float
    lint_baseline: str = "none"

    @property
    def checkout(self) -> Path:
        return self.services.workspaces.checkout(self.workspace_id)

    @classmethod
    async def create(cls, services: Services, task: Task) -> TaskEnv:
        ws = await services.workspaces.create(
            task.repo, ref=task.base_commit, issue_number=task.issue_number
        )
        # gh=None and use_ci=False: evaluation must not depend on live API state, and
        # grading needs per-test results, so tasks pin the ecosystem's default test runner.
        playbook = await services.build_playbook(
            ws,
            gh=None,
            use_ci=False,
            install_command=task.install_command,
            test_command=task.test_command,
            lint_command=task.lint_command,
        )
        setup = await services.run_check(ws.workspace_id, "setup")
        if not setup.ok:
            services.workspaces.delete(ws.workspace_id)
            raise GradingError(f"Environment setup failed:\n{setup.output_tail[-1500:]}")
        env = cls(services, task, ws.workspace_id, playbook, setup.duration_s)
        if task.lint_baseline is not None:
            env.lint_baseline = task.lint_baseline
        else:  # not measured at validation time: measure now, before anything is edited
            env.lint_baseline = {True: "clean", False: "dirty", None: "none"}[await env.run_lint()]
        return env

    async def run_lint(self) -> bool | None:
        """Run the repo's own linter on the current tree.

        Returns None when there is nothing to judge: the repo has no linter, the linter
        is not installed in the environment (shell exit 126/127), or it timed out.
        """
        if not self.playbook.tooling.lint_command:
            return None
        result = await run_check(
            self.services.runner,
            self.checkout,
            self.playbook,
            "lint",
            timeout_s=self.services.settings.check_timeout_s,
        )
        if result.timed_out or result.exit_code in (126, 127):
            return None
        return result.ok

    async def apply_patch(self, patch: str) -> None:
        if not patch.strip():
            return
        try:
            await git("apply", "--whitespace=nowarn", "-", cwd=self.checkout, stdin=patch)
        except WorkspaceError as exc:
            raise GradingError(f"Patch does not apply: {exc}") from exc

    async def install_reference_tests(self) -> None:
        """Put the maintainers' tests in place, discarding any agent edits to those files."""
        base = self.task.base_commit
        for path in self.task.test_files:
            target = self.checkout / path
            if await _exists_at(self.checkout, base, path):
                await git("checkout", "-q", base, "--", path, cwd=self.checkout)
            elif target.exists():
                await git(
                    "rm",
                    "-q",
                    "-f",
                    "--cached",
                    "--ignore-unmatch",
                    path,
                    cwd=self.checkout,
                    check=False,
                )
                target.unlink()
        await self.apply_patch(self.task.test_patch)

    async def run_reference_tests(self) -> TestOutcome:
        result = await run_check(
            self.services.runner,
            self.checkout,
            self.playbook,
            "test",
            test_ids=self.task.test_files,
            timeout_s=self.services.settings.check_timeout_s,
        )
        if result.timed_out:
            raise GradingError("Reference tests timed out.")
        return result.tests or TestOutcome()

    def close(self) -> None:
        self.services.workspaces.delete(self.workspace_id)


async def _exists_at(checkout: Path, commit: str, path: str) -> bool:
    out = await git("ls-tree", "--name-only", commit, "--", path, cwd=checkout, check=False)
    return bool(out.strip())


async def validate_task(services: Services, task: Task, *, repeats: int = 2) -> Task:
    """Prove a mined task is usable and fill in fail_to_pass / pass_to_pass.

    Runs the reference tests before the fix, then `repeats` times after it. A test is
    kept only if its outcome is identical on every post-fix run, which filters flaky tests.
    """
    env = await TaskEnv.create(services, task)
    try:
        await env.install_reference_tests()
        before = set((await env.run_reference_tests()).passed)
        await env.apply_patch(task.gold_patch)
        after: set[str] | None = None
        for i in range(max(1, repeats)):
            if i:
                # Some suites put the current time in a test id. Spacing the runs makes
                # those ids differ, so the intersection below drops them.
                await asyncio.sleep(REPEAT_GAP_S)
            passed = set((await env.run_reference_tests()).passed)
            after = passed if after is None else after & passed
        assert after is not None
        fail_to_pass = sorted(after - before)
        if not fail_to_pass:
            raise GradingError("The fix does not turn any failing reference test green.")
        return task.model_copy(
            update={
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": sorted(after & before),
                "lint_baseline": env.lint_baseline,
                "validated": True,
            }
        )
    finally:
        env.close()


@dataclass
class Grade:
    resolved: bool
    fail_to_pass_passed: int
    pass_to_pass_passed: int
    model_files: list[str]
    model_patch: str


async def grade_attempt(env: TaskEnv) -> Grade:
    task = env.task
    diff = await env.services.workspaces.diff(env.workspace_id, max_chars=10**9)
    model_files = [f for f in split_patch(diff.diff) if not is_test_path(f)]
    await env.install_reference_tests()
    passed = set((await env.run_reference_tests()).passed)
    f2p = sum(t in passed for t in task.fail_to_pass)
    p2p = sum(t in passed for t in task.pass_to_pass)
    return Grade(
        resolved=f2p == len(task.fail_to_pass)
        and p2p == len(task.pass_to_pass)
        and bool(task.fail_to_pass),
        fail_to_pass_passed=f2p,
        pass_to_pass_passed=p2p,
        model_files=model_files,
        model_patch=diff.diff,
    )
