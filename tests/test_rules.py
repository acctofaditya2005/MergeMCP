"""The rules file is the extension point, so its loading and merging get their own tests."""

import pytest
from pydantic import ValidationError

from merge_mcp.rules import deep_merge, glob_to_regex, load_rules


def test_defaults_load_and_validate(rules):
    assert set(rules.ecosystems) >= {"python", "node", "go", "rust"}
    assert rules.viability.weights["assigned"] < 0
    assert rules.precedents.trigger == "source"


def test_user_file_merges_over_defaults(tmp_path, rules):
    override = tmp_path / "rules.toml"
    override.write_text("""
[viability]
good_at = 85

[viability.weights]
assigned = -100

[ecosystems.elixir]
markers = ["mix.exs"]
default_version = "1.17"
image = "elixir:{version}"
install = "mix deps.get"
test = "mix test"

[ecosystems.node]
test = "pnpm test"

[categories]
tests = ["spec/**"]
""")
    merged = load_rules(override)
    assert merged.viability.good_at == 85
    assert merged.viability.weights["assigned"] == -100
    assert merged.viability.weights["recent_claim"] == rules.viability.weights["recent_claim"]
    assert merged.ecosystems["elixir"].test == "mix test"  # new language, no code
    assert merged.ecosystems["node"].test == "pnpm test"  # one key replaced...
    assert merged.ecosystems["node"].install == rules.ecosystems["node"].install  # ...rest kept
    assert merged.categorize("tests/test_x.py") == "source"  # lists replace, not append
    assert merged.categorize("spec/x_spec.rb") == "tests"


def test_later_files_win_and_missing_files_are_ignored(tmp_path):
    a, b = tmp_path / "a.toml", tmp_path / "b.toml"
    a.write_text("[viability]\ngood_at = 80\n")
    b.write_text("[viability]\ngood_at = 90\n")
    assert load_rules(a, tmp_path / "missing.toml", b, None).viability.good_at == 90


def test_typos_fail_loudly(tmp_path):
    bad = tmp_path / "rules.toml"
    bad.write_text("[viability]\ngood_att = 80\n")
    with pytest.raises(ValidationError, match="good_att"):
        load_rules(bad)
    bad.write_text('[ecosystems.zig]\nmarkers = ["build.zig"]\n')  # missing required keys
    with pytest.raises(ValidationError):
        load_rules(bad)


def test_deep_merge_does_not_mutate_inputs():
    base = {"a": {"x": 1, "y": 2}, "b": [1]}
    merged = deep_merge(base, {"a": {"y": 3}, "b": [2]})
    assert merged == {"a": {"x": 1, "y": 3}, "b": [2]}
    assert base == {"a": {"x": 1, "y": 2}, "b": [1]}


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("tests/**", "tests/a/b.py", True),
        ("tests/**", "src/tests.py", False),
        ("**/*_test.go", "pkg/sub/x_test.go", True),
        ("**/*_test.go", "x_test.go", True),
        ("*.md", "docs/a.md", False),  # * does not cross a slash
        ("**/*.md", "docs/a.md", True),
        ("CHANGELOG*", "CHANGELOG.rst", True),
        ("**/*.test.*", "web/src/app.test.tsx", True),
    ],
)
def test_glob_semantics(pattern, path, expected):
    assert bool(glob_to_regex(pattern).match(path)) is expected


@pytest.mark.parametrize(
    ("path", "category"),
    [
        ("src/pkg/core.py", "source"),
        ("lib/index.ts", "source"),
        ("tests/test_core.py", "tests"),
        ("pkg/core_test.go", "tests"),
        ("web/__tests__/a.js", "tests"),
        ("src/app.spec.ts", "tests"),
        ("CHANGELOG.md", "changelog"),
        ("changes/123.bugfix.rst", "changelog"),
        (".changeset/odd-cats.md", "changelog"),
        ("docs/guide.md", "docs"),
        ("README.md", "docs"),
        (".github/workflows/ci.yml", "ci"),
        ("package-lock.json", "dependencies"),
        ("sub/Cargo.lock", "dependencies"),
    ],
)
def test_file_categories_cover_several_languages(rules, path, category):
    assert rules.categorize(path) == category


def test_ecosystem_falls_back_to_file_extensions(tmp_path, rules):
    (tmp_path / "pkg").mkdir()
    for name in ("a.go", "b.go", "pkg/c.go", "script.py"):
        (tmp_path / name).write_text("")
    assert rules.ecosystem_for_repo(tmp_path) == "go"
    (tmp_path / "pyproject.toml").write_text("")  # a marker always beats extension counting
    assert rules.ecosystem_for_repo(tmp_path) == "python"
