"""Shared fixtures. Everything here is hermetic: no network, no Docker.

`fixture_repo` builds a tiny git repository with a real bug and a real fix, which gives the
workspace, checks, grading and agent tests something true to run against.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from merge_mcp.config import Settings
from merge_mcp.eval.tasks import Task
from merge_mcp.rules import Rules, load_rules
from merge_mcp.sandbox import LocalRunner
from merge_mcp.server import Services, build_services

BUGGY = '''\
"""Tiny text helpers."""


def slugify(text: str) -> str:
    """Turn a title into a URL slug."""
    return "-".join(text.lower().split())


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
'''
FIXED = BUGGY.replace(
    'return "-".join(text.lower().split())',
    'words = "".join(c if c.isalnum() else " " for c in text.lower()).split()\n'
    '    return "-".join(words)',
)
TESTS_V1 = """\
from textkit import slugify, truncate


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_truncate():
    assert truncate("abcdef", 4) == "abc…"
"""
TESTS_V2 = (
    TESTS_V1
    + """

def test_slugify_strips_punctuation():
    assert slugify("Hello, World!") == "hello-world"
"""
)
CONTRIBUTING = """\
# Contributing

## Commits
All commits must be signed off (`git commit -s`) per the DCO.

## AI policy
AI-assisted contributions are welcome if you disclose them and review every line.
"""


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path: Path) -> dict[str, str]:
    repo = tmp_path / "textkit"
    (repo / "tests").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    (repo / "textkit.py").write_text(BUGGY)
    (repo / "tests" / "test_textkit.py").write_text(TESTS_V1)
    (repo / "CONTRIBUTING.md").write_text(CONTRIBUTING)
    (repo / "ruff.toml").write_text("line-length = 100\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add slugify and truncate")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "textkit.py").write_text(FIXED)
    (repo / "tests" / "test_textkit.py").write_text(TESTS_V2)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix: strip punctuation in slugify (#7)\n\nFixes #7")
    return {
        "path": str(repo),
        "base": base,
        "head": _git(repo, "rev-parse", "HEAD"),
        "gold_patch": _git(repo, "diff", base, "HEAD", "--", "textkit.py") + "\n",
        "test_patch": _git(repo, "diff", base, "HEAD", "--", "tests") + "\n",
    }


@pytest.fixture(scope="session")
def rules() -> Rules:
    return load_rules()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        home=tmp_path / "home",
        sandbox="local",
        allow_local_sources=True,
        check_timeout_s=120,
        setup_timeout_s=120,
    )


@pytest.fixture
def services(settings: Settings) -> Services:
    return build_services(settings, runner=LocalRunner(settings))


@pytest.fixture
def task(fixture_repo: dict[str, str]) -> Task:
    """An unvalidated task over the fixture repo. Uses the host's pytest so it runs offline."""
    return Task(
        task_id="local__textkit-7",
        repo=fixture_repo["path"],
        base_commit=fixture_repo["base"],
        issue_number=7,
        problem_statement="slugify keeps punctuation\n\nslugify('Hello, World!') returns "
        "'hello,-world!' but should return 'hello-world'.",
        gold_patch=fixture_repo["gold_patch"],
        test_patch=fixture_repo["test_patch"],
        test_files=["tests/test_textkit.py"],
        install_command="true",
        test_command=f"{sys.executable} -m pytest",
        lint_command=f"{sys.executable} -m ruff check --no-cache .",
    )
