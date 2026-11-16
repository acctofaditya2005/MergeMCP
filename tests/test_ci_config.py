"""Reading commands out of CI workflows, for several languages, without per-language code."""

from pathlib import Path

from merge_mcp.ci_config import (
    logical_lines,
    pick_matrix_value,
    read_ci,
    split_script,
    substitute,
)


def workflow(root: Path, text: str, name: str = "ci.yml") -> None:
    path = root / ".github" / "workflows" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_python_matrix_workflow(tmp_path, rules):
    workflow(
        tmp_path,
        """
name: tests
on: [push]
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [windows-latest, ubuntu-latest, macos-latest]
        python-version: ["3.10", "3.13", "3.14-dev", "pypy3.10"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e ".[dev]"
      - run: pytest -q --cov
      - run: ruff check .
""",
    )
    plan = read_ci(tmp_path, rules)
    assert (plan.workflow, plan.job) == (".github/workflows/ci.yml", "test")
    assert (plan.ecosystem, plan.version) == ("python", "3.13")  # newest stable, not -dev/pypy
    assert plan.install == ['python -m pip install --upgrade pip\npip install -e ".[dev]"']
    assert plan.test == ["pytest -q --cov"]
    assert plan.lint == ["ruff check ."]


def test_one_step_that_installs_then_tests_is_split(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: 22
      - run: |
          npm ci
          export NODE_OPTIONS=--max-old-space-size=4096
          npm test -- --ci
          npm run lint
""",
    )
    plan = read_ci(tmp_path, rules)
    assert (plan.ecosystem, plan.version) == ("node", "22")
    assert plan.install == ["npm ci"]
    # The export travels with the command that follows it.
    assert plan.test == ["export NODE_OPTIONS=--max-old-space-size=4096\nnpm test -- --ci"]
    assert plan.lint == ["npm run lint"]


def test_go_and_rust_need_no_extra_code(tmp_path, rules):
    go, rust = tmp_path / "go", tmp_path / "rust"
    workflow(
        go,
        """
jobs:
  unit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-go@v5
        with: {go-version: "1.23"}
      - run: go mod download
      - run: go vet ./...
      - run: go test -race ./...
""",
    )
    workflow(
        rust,
        """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: dtolnay/rust-toolchain@stable
      - run: cargo build --locked
      - run: cargo test --all-features
      - run: cargo clippy -- -D warnings
""",
    )
    g = read_ci(go, rules)
    assert (g.ecosystem, g.version, g.install, g.test, g.lint) == (
        "go",
        "1.23",
        ["go mod download"],
        ["go test -race ./..."],
        ["go vet ./..."],
    )
    r = read_ci(rust, rules)
    assert (r.ecosystem, r.install, r.test) == (
        "rust",
        ["cargo build --locked"],
        ["cargo test --all-features"],
    )
    assert r.version is None  # '@stable' carries no version input; the default image is used


def test_steps_that_cannot_run_in_a_sandbox_are_dropped_and_reported(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: sudo apt-get install -y libxml2-dev
      - run: pip install -e .
      - run: pytest --token=${{ secrets.API_TOKEN }}
      - run: pytest tests/unit
      - run: codecov --file coverage.xml
      - if: runner.os == 'Windows'
        run: pytest tests/windows
""",
    )
    plan = read_ci(tmp_path, rules)
    assert plan.install == ["pip install -e ."]
    assert plan.test == ["pytest tests/unit"]
    assert len(plan.skipped) == 3
    assert any("sudo" in s for s in plan.skipped) and any("secrets" in s for s in plan.skipped)


def test_jobs_for_other_operating_systems_are_ignored(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  windows:
    runs-on: windows-latest
    steps:
      - run: pytest
""",
    )
    assert read_ci(tmp_path, rules) is None


def test_test_job_is_preferred_over_other_jobs_and_files(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  docs:
    runs-on: ubuntu-latest
    steps:
      - run: make check
""",
        name="a-docs.yml",
    )
    workflow(
        tmp_path,
        """
jobs:
  unit-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v5
        with: {python-version: "3.12"}
      - run: tox -e py
""",
        name="b-tests.yml",
    )
    plan = read_ci(tmp_path, rules)
    assert (plan.workflow, plan.job, plan.test) == (
        ".github/workflows/b-tests.yml",
        "unit-tests",
        ["tox -e py"],
    )


def test_working_directory_is_respected(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: npm ci
        working-directory: web
      - run: npm test
        working-directory: web
""",
    )
    plan = read_ci(tmp_path, rules)
    assert plan.install == ["(cd web && { npm ci; })"]
    assert plan.test == ["(cd web && { npm test; })"]


def test_no_workflows_or_broken_yaml(tmp_path, rules):
    assert read_ci(tmp_path, rules) is None
    workflow(tmp_path, "jobs: [this is: not valid")
    workflow(tmp_path, "just a string", name="other.yml")
    assert read_ci(tmp_path, rules) is None


def test_helpers(rules):
    assert pick_matrix_value(["3.9", "3.12", "3.13-dev"], rules.ci) == "3.12"
    assert pick_matrix_value(["macos-14", "ubuntu-22.04"], rules.ci) == "ubuntu-22.04"
    assert pick_matrix_value([18, 20, 22], rules.ci) == 22
    assert pick_matrix_value("single", rules.ci) == "single"
    assert (
        substitute("py${{ matrix.v }}-${{ github.sha }}", {"v": "3.12"})
        == "py3.12-${{ github.sha }}"
    )
    assert logical_lines("# comment\npip install \\\n  -e .\n\npytest") == [
        "pip install    -e .",
        "pytest",
    ]
    kinds, skipped = split_script("cd pkg\ngo test ./...", rules.ci)
    assert kinds["test"] == ["cd pkg", "go test ./..."] and skipped == []


# ---- lessons from running against real repositories --------------------------------


def test_everyday_job_beats_specialised_and_scheduled_jobs(tmp_path, rules):
    workflow(
        tmp_path,
        """
on:
  schedule:
    - cron: "0 3 * * *"
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: cargo test --all-features -- --ignored
""",
        name="nightly.yml",
    )
    workflow(
        tmp_path,
        """
on: [push, pull_request]
jobs:
  loom:
    runs-on: ubuntu-latest
    steps:
      - run: cargo test --lib --cfg loom
  cross:
    runs-on: ubuntu-latest
    steps:
      - run: cargo test --target i686-unknown-linux-gnu
  stable:
    runs-on: ubuntu-latest
    steps:
      - run: ci/test-stable.sh test
""",
    )
    plan = read_ci(tmp_path, rules)
    assert (plan.workflow, plan.job) == (".github/workflows/ci.yml", "stable")
    assert plan.test == ["ci/test-stable.sh test"]  # a project test script counts as a test


def test_matrix_written_as_a_list_of_combinations(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - {build: win, os: windows-latest, rust: stable}
          - {build: nightly, os: ubuntu-latest, rust: nightly}
          - {build: pinned, os: ubuntu-latest, rust: "1.85"}
    steps:
      - uses: dtolnay/rust-toolchain@master
        with:
          toolchain: ${{ matrix.rust }}
      - run: cargo test --verbose
""",
    )
    plan = read_ci(tmp_path, rules)
    assert (plan.ecosystem, plan.version, plan.test) == ("rust", "1.85", ["cargo test --verbose"])


def test_literal_env_is_exported_and_github_context_is_not(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      GOFLAGS: -mod=readonly
      TOKEN: ${{ secrets.TOKEN }}
    steps:
      - run: make test
        env:
          TESTTAGS: "-tags sonic"
""",
    )
    plan = read_ci(tmp_path, rules)
    assert plan.test == ["export GOFLAGS=-mod=readonly\nexport TESTTAGS='-tags sonic'\nmake test"]
    assert "TOKEN" not in plan.test[0]


def test_lint_is_borrowed_from_a_sibling_job(tmp_path, rules):
    workflow(
        tmp_path,
        """
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: npm run lint
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
      - run: npm install
      - run: npm run test-ci
""",
    )
    plan = read_ci(tmp_path, rules)
    assert (plan.job, plan.test, plan.lint) == ("test", ["npm run test-ci"], ["npm run lint"])


def test_more_runner_spellings_are_recognised(rules):
    for command in (
        "make richtest",
        "npm run unit",
        "pnpm run test:ci",
        "just test-all",
        "./scripts/run_tests.sh",
        "bundle exec rspec",
    ):
        assert rules.ci.classify(command) == "test", command
    for command in ("echo done", "git status", "ls -la"):
        assert rules.ci.classify(command) is None, command
