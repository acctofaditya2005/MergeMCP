"""Build a repo "playbook": how this project expects contributions to look.

Everything here is derived by rules from files in the checkout, its CI configuration,
`git log`, and (when the API is reachable) recently merged pull requests. No LLM is
involved: the playbook for a given commit is reproducible, cheap, and safe to cache.

Which commands to run is resolved in this order:

1. explicit overrides from the caller;
2. the repo's CI workflow (`ci_config.read_ci`), which works for any language;
3. the matching `[ecosystems.*]` rule, refined by an optional detector hook.
"""

from __future__ import annotations

import re
import statistics
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import precedents
from .ci_config import read_ci
from .github import GitHubClient, GitHubError
from .models import (
    CommitConventions,
    ContributionPolicy,
    Playbook,
    PullRequestConventions,
    Tooling,
)
from .rules import Ecosystem, Rules
from .workspace import git

_CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|tests|build|ci|chore|revert)(\([^)]+\))?!?: \S"
)
_SCOPED = re.compile(r"^[A-Za-z0-9_./-]{2,30}: \S")
_TEST_EXTRAS = ("test", "tests", "testing", "dev", "develop")
_REQ_FILES = (
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements-tests.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
    "requirements/dev.txt",
    "requirements/test.txt",
    "requirements/tests.txt",
)


def _first_existing(root: Path, candidates: list[str]) -> str | None:
    return next((c for c in candidates if (root / c).is_file()), None)


def _load_pyproject(root: Path) -> dict[str, Any]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return {}


def python_detector(root: Path, eco: Ecosystem) -> dict[str, Any]:
    """Refine the Python defaults from packaging metadata (extras, groups, linter config)."""
    pyproject = _load_pyproject(root)
    project = pyproject.get("project", {})
    tool = pyproject.get("tool", {})
    seen: list[str] = []
    install: list[str] = ["python -m pip install -q -U pip"]  # old pips lack --group

    if pyproject:
        seen.append("pyproject.toml")
    has_package = (
        bool(project)
        or (root / "setup.py").is_file()
        or (root / "setup.cfg").is_file()
        or "poetry" in tool
        or "flit" in tool
    )
    if (root / "setup.py").is_file():
        seen.append("setup.py")

    package: str | None = None
    if has_package:
        extras = [e for e in _TEST_EXTRAS if e in project.get("optional-dependencies", {})]
        package = f'pip install -e ".[{",".join(extras)}]"' if extras else "pip install -e ."
    groups = [g for g in _TEST_EXTRAS if g in pyproject.get("dependency-groups", {})]
    if groups and pyproject:
        install.append("pip install " + " ".join(f"--group {g}" for g in groups))
    for req in _REQ_FILES:
        if (root / req).is_file():
            install.append(f"pip install -r {req}")
            seen.append(req)
    if len(install) == 1 and package is None and (root / "requirements.txt").is_file():
        install.append("pip install -r requirements.txt")
        seen.append("requirements.txt")

    lint: str | None = None
    lint_pkg: str | None = None
    setup_cfg = (
        (root / "setup.cfg").read_text(errors="replace") if (root / "setup.cfg").is_file() else ""
    )
    if "ruff" in tool or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        lint, lint_pkg = "ruff check .", "ruff"
    elif (root / ".flake8").is_file() or "[flake8]" in setup_cfg:
        lint, lint_pkg = "flake8", "flake8"
    elif "black" in tool:
        lint, lint_pkg = "black --check .", "black"
    install.append("pip install pytest" + (f" {lint_pkg}" if lint_pkg else ""))
    if package:
        # Last on purpose: a dev dependency can pull the project itself from PyPI (pytest
        # depends on iniconfig, for example) and would shadow the checkout otherwise.
        install.append(package)
    return {
        "install": " && ".join(install),
        "test": eco.test,
        "lint": lint,
        "detected_from": seen,
        "python_requires": project.get("requires-python"),
    }


#: Named hooks an ecosystem rule can ask for with `detector = "<name>"`.
DETECTORS: dict[str, Callable[[Path, Ecosystem], dict[str, Any]]] = {"python": python_detector}


def resolve_tooling(
    root: Path,
    rules: Rules,
    *,
    use_ci: bool = True,
    install_command: str | None = None,
    test_command: str | None = None,
    lint_command: str | None = None,
) -> Tooling:
    """Work out how to install, test and lint the repo at `root`."""
    plan = read_ci(root, rules) if use_ci else None
    name = (plan.ecosystem if plan else None) or rules.ecosystem_for_repo(root)
    eco = rules.ecosystems.get(name) if name else None

    base: dict[str, Any] = {"install": "", "test": "", "lint": None, "detected_from": []}
    if eco is not None:
        base.update(install=eco.install, test=eco.test, lint=eco.lint)
        base["detected_from"] = [m for m in eco.markers if (root / m).is_file()]
        if eco.detector:
            base.update(DETECTORS[eco.detector](root, eco))
    source = f"defaults: {name}" if eco else "none"

    install, test, lint = base["install"], base["test"], base["lint"]
    skipped: list[str] = []
    if plan is not None:
        ci_test = "\n".join(plan.test)
        if eco is None or eco.trusts_ci_test(ci_test):
            test = ci_test
            install = "\n".join(plan.install) or install
            source = f"ci: {plan.workflow} (job: {plan.job})"
            skipped = plan.skipped
        else:
            source += f" (CI job '{plan.job}' runs tests through a wrapper)"
        lint = lint or "\n".join(plan.lint) or None
        base["detected_from"] = [*base["detected_from"], plan.workflow]
    if install_command or test_command or lint_command:
        install, test = install_command or install, test_command or test
        lint = lint_command or lint
        source = "override"

    for marker in ("tox.ini", "noxfile.py", "Makefile", "justfile", ".pre-commit-config.yaml"):
        if (root / marker).is_file() and marker not in base["detected_from"]:
            base["detected_from"].append(marker)

    single_line = "\n" not in test
    accepts_ids = bool(eco and single_line and eco.accepts_test_ids(test))
    return Tooling(
        ecosystem=name if eco else None,
        toolchain_version=plan.version if plan else None,
        image=eco.image_for(plan.version if plan else None) if eco else None,
        source=source,
        python_requires=base.get("python_requires"),
        env_setup=eco.env_setup if eco else None,
        env=dict(eco.env) if eco else {},
        install_command=install or "true",
        test_command=test,
        lint_command=lint,
        test_flags=eco.test_flags if accepts_ids and eco else "",
        accepts_test_ids=accepts_ids,
        result_parser=eco.result_parser if accepts_ids and eco else None,
        detected_from=base["detected_from"],
        uses_pre_commit=(root / ".pre-commit-config.yaml").is_file(),
        skipped_ci_steps=skipped[:20],
    )


def detect_policy(root: Path, rules: Rules) -> ContributionPolicy:
    cfg = rules.policy
    contributing = _first_existing(root, cfg.contributing_paths)
    template = _first_existing(root, cfg.pr_template_paths)
    text = ""
    for rel in (contributing, template, *cfg.extra_policy_paths):
        if rel and (root / rel).is_file():
            text += (root / rel).read_text(encoding="utf-8", errors="replace") + "\n"

    sections = [
        m.group(2).strip() for m in re.finditer(r"^(#{1,3})\s+(.+?)\s*#*$", text, re.MULTILINE)
    ][:20]
    ai = re.compile(cfg.ai_pattern, re.IGNORECASE)
    return ContributionPolicy(
        contributing_path=contributing,
        pr_template_path=template,
        code_of_conduct=_first_existing(root, cfg.code_of_conduct_paths) is not None,
        requires_dco_signoff=bool(re.search(cfg.dco_pattern, text, re.IGNORECASE)),
        requires_cla=bool(re.search(cfg.cla_pattern, text, re.IGNORECASE)),
        ai_policy_mentions=[ln.strip()[:240] for ln in text.splitlines() if ai.search(ln)][:5],
        key_sections=sections,
    )


def analyze_commits(log: str) -> CommitConventions | None:
    """`log` is `git log --format=%s%x1f%b%x1e`: subject, unit sep, body, record sep."""
    subjects, signed = [], 0
    for record in log.split("\x1e"):
        subject, _, body = record.strip("\n").partition("\x1f")
        subject = subject.strip()
        if not subject or subject.startswith(("Merge ", "Bump ")):
            continue
        subjects.append(subject)
        signed += "Signed-off-by:" in body
    if not subjects:
        return None
    n = len(subjects)
    conventional = sum(bool(_CONVENTIONAL.match(s)) for s in subjects) / n
    scoped = sum(bool(_SCOPED.match(s)) and not _CONVENTIONAL.match(s) for s in subjects) / n
    style = (
        "conventional" if conventional >= 0.5 else "scoped-prefix" if scoped >= 0.5 else "free-form"
    )
    return CommitConventions(
        sample_size=n,
        conventional_commits_ratio=round(conventional, 2),
        scoped_prefix_ratio=round(scoped, 2),
        median_subject_length=int(statistics.median(len(s) for s in subjects)),
        signed_off_ratio=round(signed / n, 2),
        style=style,
        examples=subjects[:5],
    )


def analyze_pulls(pulls: list[dict[str, Any]]) -> PullRequestConventions | None:
    merged = [p for p in pulls if p.get("merged_at")]
    if not merged:
        return None
    refs = sum(
        bool(
            re.search(
                r"(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+#\d+|#\d+",
                (p.get("body") or "") + " " + p.get("title", ""),
                re.IGNORECASE,
            )
        )
        for p in merged
    )
    return PullRequestConventions(
        sample_size=len(merged),
        title_examples=[p["title"] for p in merged[:5]],
        branch_examples=[p["head"]["ref"] for p in merged[:5] if p.get("head")],
        median_files_changed=None,
        references_issue_ratio=round(refs / len(merged), 2),
    )


async def build_playbook(
    checkout: Path,
    rules: Rules,
    *,
    repo: str,
    base_commit: str,
    gh: GitHubClient | None = None,
    use_ci: bool = True,
    install_command: str | None = None,
    test_command: str | None = None,
    lint_command: str | None = None,
) -> Playbook:
    notes: list[str] = []
    tooling = resolve_tooling(
        checkout,
        rules,
        use_ci=use_ci,
        install_command=install_command,
        test_command=test_command,
        lint_command=lint_command,
    )
    if tooling.source == "override":
        notes.append("Install/test commands were overridden by the caller.")
    if not tooling.test_command:
        notes.append(
            "Could not work out how to test this repository. Pass test_command to start_workspace."
        )
    if tooling.skipped_ci_steps:
        notes.append(
            f"{len(tooling.skipped_ci_steps)} CI command(s) cannot run in the sandbox "
            "and were left out; setup may be incomplete."
        )
    policy = detect_policy(checkout, rules)
    log = await git(
        "log", "-n", "50", "--no-merges", "--format=%s%x1f%b%x1e", cwd=checkout, check=False
    )
    commits = analyze_commits(log)
    learned = await precedents.mine(checkout, rules)

    pulls = None
    if gh is not None and not repo.startswith("local/"):
        try:
            pulls = analyze_pulls(await gh.closed_pulls(repo))
        except GitHubError as exc:
            notes.append(f"Pull request conventions unavailable: {exc}")

    if policy.ai_policy_mentions:
        notes.append(
            "Contributor docs mention AI-generated contributions; read them before "
            "opening a pull request."
        )
    if policy.requires_dco_signoff:
        notes.append("Commits need a Signed-off-by line (use `git commit -s`).")
    if policy.requires_cla:
        notes.append("A Contributor License Agreement must be signed by the human author.")
    if tooling.uses_pre_commit:
        notes.append("The repo uses pre-commit; run `pre-commit run --all-files` before pushing.")
    for rule in learned.rules if learned else []:
        notes.append(
            f"{rule.confidence:.0%} of past {rule.when} changes also changed {rule.expect} files."
        )
    return Playbook(
        repo=repo,
        base_commit=base_commit,
        tooling=tooling,
        commits=commits,
        pull_requests=pulls,
        precedents=learned,
        policy=policy,
        notes=notes,
    )
