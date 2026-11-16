"""Benchmark task format (one JSON object per file) and patch helpers.

A task is a real, already-solved issue: the repo state before the fix (`base_commit`),
the issue text, the maintainers' fix (`gold_patch`) and the tests they added
(`test_patch`). An attempt is graded only by running those tests:

* `fail_to_pass`: tests that fail before the fix and pass after it. All must pass.
* `pass_to_pass`: tests that passed before and must still pass (no regressions).

The agent never sees `gold_patch` or `test_patch`.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, Field

_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)
_TEST_DIRS = {"test", "tests", "testing"}


class Task(BaseModel):
    task_id: str
    repo: str = Field(description="'owner/name' on GitHub, or a local path for fixtures.")
    base_commit: str
    issue_number: int | None = None
    problem_statement: str
    gold_patch: str
    test_patch: str
    test_files: list[str]
    fail_to_pass: list[str] = Field(default_factory=list)
    pass_to_pass: list[str] = Field(default_factory=list)
    install_command: str | None = None
    test_command: str | None = None
    lint_command: str | None = None
    #: The repo's own linter on the untouched base commit: "clean", "dirty", or "none"
    #: when there is no linter or it cannot run. Unset means not measured yet. Only
    #: tasks with a clean baseline are graded for lint, so an agent is never blamed for
    #: problems that were already there.
    lint_baseline: Literal["clean", "dirty", "none"] | None = None
    source_commit: str | None = None
    validated: bool = False

    @property
    def gold_files(self) -> list[str]:
        return sorted(split_patch(self.gold_patch))


def is_test_path(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return (
        bool(_TEST_DIRS & set(p.parts[:-1]))
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


def split_patch(diff: str) -> dict[str, str]:
    """Split a unified diff into {new path: that file's diff chunk}."""
    chunks: dict[str, str] = {}
    headers = list(_DIFF_HEADER.finditer(diff))
    for i, m in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(diff)
        chunks[m.group(2)] = diff[m.start() : end]
    return chunks


def load_tasks(path: Path, *, only_validated: bool = True) -> list[Task]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    tasks = [Task.model_validate_json(f.read_text()) for f in files]
    if only_validated:
        tasks = [t for t in tasks if t.validated]
    return tasks


def save_task(task: Task, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    out = directory / f"{task.task_id}.json"
    out.write_text(task.model_dump_json(indent=2) + "\n")
    return out
