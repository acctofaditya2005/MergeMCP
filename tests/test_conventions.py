from pathlib import Path

from merge_mcp.conventions import (
    analyze_commits,
    analyze_pulls,
    build_playbook,
    detect_policy,
    resolve_tooling,
)


def write(root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# ---- command resolution: overrides > CI > ecosystem rule ----------------------------


def test_python_defaults_from_pyproject_extras_and_ruff(tmp_path, rules):
    write(
        tmp_path,
        "pyproject.toml",
        """
[project]
name = "x"
requires-python = ">=3.10"
[project.optional-dependencies]
tests = ["pytest"]
docs = ["sphinx"]
[tool.ruff]
line-length = 88
""",
    )
    t = resolve_tooling(tmp_path, rules)
    assert (t.ecosystem, t.source, t.image) == ("python", "defaults: python", "python:3.12")
    assert t.python_requires == ">=3.10"
    assert t.lint_command == "ruff check ."
    assert t.install_command.endswith('pip install -e ".[tests]"')
    assert "pip install pytest ruff" in t.install_command
    assert t.accepts_test_ids and t.result_parser == "pytest"
    assert t.env["PYTHONHASHSEED"] == "0"


def test_editable_install_runs_last(tmp_path, rules):
    """A dev dependency may pull the project from PyPI and shadow the checkout otherwise."""
    write(
        tmp_path,
        "pyproject.toml",
        """
[project]
name = "x"
[dependency-groups]
dev = ["pytest"]
""",
    )
    steps = resolve_tooling(tmp_path, rules).install_command.split(" && ")
    assert steps[-1] == "pip install -e ."
    assert "pip install --group dev" in steps


def test_python_requirements_only_and_flake8(tmp_path, rules):
    write(tmp_path, "requirements.txt", "requests\n")
    write(tmp_path, "requirements-dev.txt", "pytest\n")
    write(tmp_path, ".flake8", "[flake8]\n")
    t = resolve_tooling(tmp_path, rules)
    assert "pip install -r requirements-dev.txt" in t.install_command
    assert "pip install -e" not in t.install_command
    assert t.lint_command == "flake8"


def test_broken_pyproject_does_not_crash(tmp_path, rules):
    write(tmp_path, "pyproject.toml", "this is [not toml")
    assert resolve_tooling(tmp_path, rules).test_command == "python -m pytest"


def test_other_ecosystems_come_from_rules_alone(tmp_path, rules):
    """No per-language code: node, go and rust are entirely described by the rules file."""
    for marker, name, test, image in (
        ("package.json", "node", "npm test", "node:lts"),
        ("go.mod", "go", "go test ./...", "golang:1"),
        ("Cargo.toml", "rust", "cargo test", "rust:1"),
    ):
        repo = tmp_path / name
        write(repo, marker, "{}" if marker.endswith("json") else "")
        t = resolve_tooling(repo, rules)
        assert (t.ecosystem, t.test_command, t.image) == (name, test, image)
        assert not t.accepts_test_ids and t.result_parser is None
    assert resolve_tooling(tmp_path / "go", rules).env["GOPATH"] == "$PWD/.merge-mcp-cache/go"


def test_unknown_repo_has_no_test_command(tmp_path, rules):
    t = resolve_tooling(tmp_path, rules)
    assert (t.ecosystem, t.source, t.test_command, t.image) == (None, "none", "", None)


def test_ci_beats_defaults_and_overrides_beat_ci(tmp_path, rules):
    write(tmp_path, "package.json", "{}")
    write(
        tmp_path,
        ".github/workflows/ci.yml",
        """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with: {node-version: 20}
      - run: pnpm install --frozen-lockfile
      - run: pnpm test
""",
    )
    t = resolve_tooling(tmp_path, rules)
    assert t.source == "ci: .github/workflows/ci.yml (job: test)"
    assert (t.install_command, t.test_command, t.image) == (
        "pnpm install --frozen-lockfile",
        "pnpm test",
        "node:20",
    )
    assert resolve_tooling(tmp_path, rules, use_ci=False).test_command == "npm test"
    forced = resolve_tooling(tmp_path, rules, test_command="node --test")
    assert (forced.source, forced.test_command) == ("override", "node --test")
    assert forced.install_command == "pnpm install --frozen-lockfile"


def test_a_new_ecosystem_needs_only_a_rules_entry(tmp_path, rules):
    """The extension point: add a table to the rules file, write no Python."""
    from merge_mcp.rules import Ecosystem

    extended = rules.model_copy(
        update={
            "ecosystems": {
                **rules.ecosystems,
                "elixir": Ecosystem(
                    markers=["mix.exs"],
                    default_version="1.17",
                    image="elixir:{version}",
                    install="mix deps.get",
                    test="mix test",
                    lint="mix format --check",
                ),
            }
        }
    )
    write(tmp_path, "mix.exs", "")
    t = resolve_tooling(tmp_path, extended)
    assert (t.ecosystem, t.install_command, t.test_command, t.image) == (
        "elixir",
        "mix deps.get",
        "mix test",
        "elixir:1.17",
    )


# ---- policy ------------------------------------------------------------------------


def test_policy_detection(tmp_path, rules):
    write(
        tmp_path,
        ".github/CONTRIBUTING.md",
        """# Contributing
## Sign your work
Add a Signed-off-by line. You must also sign the Contributor License Agreement.
## AI
Do not submit LLM output you have not reviewed.
""",
    )
    write(tmp_path, ".github/pull_request_template.md", "## Checklist\n- [ ] tests\n")
    write(tmp_path, "CODE_OF_CONDUCT.md", "be kind")
    p = detect_policy(tmp_path, rules)
    assert p.contributing_path == ".github/CONTRIBUTING.md"
    assert p.pr_template_path == ".github/pull_request_template.md"
    assert p.requires_dco_signoff and p.requires_cla and p.code_of_conduct
    assert len(p.ai_policy_mentions) == 1 and "LLM" in p.ai_policy_mentions[0]
    assert "Sign your work" in p.key_sections


def test_policy_empty_repo(tmp_path, rules):
    p = detect_policy(tmp_path, rules)
    assert p.contributing_path is None and not p.requires_dco_signoff
    assert p.ai_policy_mentions == []


# ---- commit and pull request style -------------------------------------------------


def log(*entries: tuple[str, str]) -> str:
    return "".join(f"{s}\x1f{b}\x1e\n" for s, b in entries)


def test_commit_style_conventional():
    c = analyze_commits(
        log(
            ("fix(parser): handle tabs", "Signed-off-by: A <a@b.c>"),
            ("feat: add x", ""),
            ("docs: typo", ""),
            ("Merge branch 'x'", ""),
        )
    )
    assert c.style == "conventional" and c.sample_size == 3
    assert c.signed_off_ratio == 0.33


def test_commit_style_scoped_and_free_form():
    scoped = analyze_commits(log(("parser: handle tabs", ""), ("cli: add flag", "")))
    assert scoped.style == "scoped-prefix"
    assert analyze_commits(log(("Handle tabs", ""), ("Add flag", ""))).style == "free-form"
    assert analyze_commits("") is None


def test_pull_request_conventions():
    pulls = [
        {"merged_at": "x", "title": "Fix tabs", "body": "Fixes #3", "head": {"ref": "fix-tabs"}},
        {"merged_at": "x", "title": "Add flag", "body": "", "head": {"ref": "flag"}},
        {"merged_at": None, "title": "Rejected", "body": "", "head": {"ref": "nope"}},
    ]
    p = analyze_pulls(pulls)
    assert p.sample_size == 2 and p.references_issue_ratio == 0.5
    assert p.branch_examples == ["fix-tabs", "flag"]
    assert analyze_pulls([]) is None


async def test_build_playbook_on_fixture_repo(fixture_repo, rules):
    pb = await build_playbook(
        Path(fixture_repo["path"]),
        rules,
        repo="local/textkit",
        base_commit=fixture_repo["head"],
        test_command="pytest -x",
    )
    assert pb.commits.style == "conventional"
    assert pb.policy.requires_dco_signoff
    assert (pb.tooling.test_command, pb.tooling.source) == ("pytest -x", "override")
    assert pb.tooling.lint_command == "ruff check ."
    assert any("Signed-off-by" in n for n in pb.notes)
    assert any("AI" in n for n in pb.notes)


def test_python_ci_wrappers_are_not_adopted(tmp_path, rules):
    """tox and `uv run` need tools the sandbox lacks; direct pytest also gives per-test data."""
    write(tmp_path, "pyproject.toml", "[project]\nname = 'x'\n")
    write(
        tmp_path,
        ".github/workflows/ci.yml",
        """
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - uses: astral-sh/setup-uv@v6
      - run: uv run --locked tox run -e py
      - run: ruff check src
""",
    )
    t = resolve_tooling(tmp_path, rules)
    assert t.source == "defaults: python (CI job 'tests' runs tests through a wrapper)"
    assert t.test_command == "python -m pytest" and t.accepts_test_ids
    assert t.lint_command == "ruff check src"  # the linter command is still worth keeping

    write(
        tmp_path,
        ".github/workflows/ci.yml",
        """
jobs:
  tests:
    runs-on: ubuntu-latest
    steps:
      - run: pip install -e .[dev]
      - run: python -m pytest -q --cov=x
""",
    )
    direct = resolve_tooling(tmp_path, rules)
    assert direct.source.startswith("ci:") and direct.test_command == "python -m pytest -q --cov=x"
    assert direct.accepts_test_ids and direct.test_flags == "-rA -p no:cacheprovider"
