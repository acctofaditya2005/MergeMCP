"""Read a repository's CI configuration to learn how it is built and tested.

A project's CI workflow is the one place that must state, correctly and for any language,
how to install dependencies, run the tests and run the linter. Reading it replaces
per-language guesswork. What counts as a "test" or "install" step, which actions set up
which toolchain, and which steps cannot be reproduced locally all come from the rules
file (`[ci]` and `[ecosystems.*]`), not from this module.

Not everything in CI can run in a sandbox: steps that need secrets, sudo, service
containers or another operating system are dropped and reported in `skipped`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .rules import CIRules, Rules

_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")
_ANY_EXPR = re.compile(r"\$\{\{.*?\}\}")
_VERSION = re.compile(r"\d+(?:\.\d+)*")


@dataclass
class CIPlan:
    workflow: str
    job: str
    ecosystem: str | None = None
    version: str | None = None
    install: list[str] = field(default_factory=list)
    test: list[str] = field(default_factory=list)
    lint: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _version_key(value: Any) -> tuple[int, ...]:
    m = _VERSION.search(str(value))
    return tuple(int(p) for p in m.group().split(".")) if m else ()


def pick_matrix_value(values: Any, rules: CIRules) -> Any:
    """Choose one value from a matrix axis: Linux over other systems, newest stable version."""
    if not isinstance(values, list) or not values:
        return values
    scalars = [v for v in values if isinstance(v, str | int | float)]
    if not scalars:
        return None
    preferred = [v for v in scalars if any(p in str(v).lower() for p in rules.prefer_matrix)]
    if preferred:
        return preferred[0]
    stable = [
        v for v in scalars if not any(u in str(v).lower() for u in rules.unstable_versions)
    ] or scalars
    versioned = [v for v in stable if _version_key(v)]
    return max(versioned, key=_version_key) if versioned else stable[0]


def substitute(text: str, context: dict[str, Any]) -> str:
    """Fill in `${{ matrix.x }}` references. Other expressions are left in place."""

    def repl(m: re.Match[str]) -> str:
        value = context.get(m.group(1))
        return m.group(0) if value is None else str(value)

    return _MATRIX_REF.sub(repl, text)


def logical_lines(script: str) -> list[str]:
    """Split a shell script into commands, joining backslash continuations."""
    lines, buffer = [], ""
    for raw in script.splitlines():
        line = raw.rstrip()
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        if buffer.strip() and not buffer.strip().startswith("#"):
            lines.append(buffer.strip())
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def split_script(script: str, rules: CIRules) -> tuple[dict[str, list[str]], list[str]]:
    """Sort a step's commands into install / test / lint.

    A single CI step often installs and then tests. Each command is classified on its
    own; commands that match nothing (`cd pkg`, `export X=1`) travel with the next
    classified command so the context they set up is preserved.
    """
    out: dict[str, list[str]] = {"install": [], "test": [], "lint": []}
    skipped: list[str] = []
    pending: list[str] = []
    last_kind: str | None = None
    for line in logical_lines(script):
        if _ANY_EXPR.search(line) or not rules.reproducible(line):
            skipped.append(line)
            continue
        kind = rules.classify(line)
        if kind is None:
            pending.append(line)
            continue
        out[kind] += [*pending, line]
        pending, last_kind = [], kind
    if pending and last_kind:
        out[last_kind] += pending
    return out, skipped


def _runner_is_skipped(runs_on: Any, rules: CIRules) -> bool:
    text = " ".join(runs_on) if isinstance(runs_on, list) else str(runs_on or "")
    return any(s in text.lower() for s in rules.skip_runners)


def matrix_context(matrix: Any, rules: CIRules) -> dict[str, Any]:
    """Collapse a job matrix to one concrete combination to reproduce."""
    if not isinstance(matrix, dict):
        return {}
    context = {
        key: pick_matrix_value(values, rules)
        for key, values in matrix.items()
        if key not in ("include", "exclude")
    }
    include = [e for e in matrix.get("include") or [] if isinstance(e, dict)]
    if include and not context:  # a matrix written only as a list of combinations
        linux = [
            e for e in include if any(p in str(e).lower() for p in rules.prefer_matrix)
        ] or include
        stable = [
            e for e in linux if not any(u in str(e).lower() for u in rules.unstable_versions)
        ] or linux
        context = dict(stable[0])
    return context


def _exports(env: Any, context: dict[str, Any]) -> list[str]:
    """Literal `env:` entries become exports; ones that need GitHub's context are dropped."""
    lines = []
    for key, value in env.items() if isinstance(env, dict) else []:
        text = substitute(str(value), context)
        if not _ANY_EXPR.search(text) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            lines.append(f"export {key}={shlex.quote(text)}")
    return lines


def _plan_for_job(workflow: str, job_id: str, job: dict[str, Any], rules: Rules) -> CIPlan | None:
    context = matrix_context((job.get("strategy") or {}).get("matrix"), rules.ci)
    if _runner_is_skipped(substitute(str(job.get("runs-on", "")), context), rules.ci):
        return None

    plan = CIPlan(workflow=workflow, job=job_id)
    job_env = _exports(job.get("env"), context)
    for step in job.get("steps") or []:
        if not isinstance(step, dict):
            continue
        condition = str(step.get("if", "")).lower()
        if any(s in condition for s in rules.ci.skip_runners):
            continue
        uses = step.get("uses")
        if isinstance(uses, str):
            eco_name = rules.ecosystem_for_action(uses)
            if eco_name and plan.ecosystem is None:
                plan.ecosystem = eco_name
                inputs = step.get("with") or {}
                for key in rules.ecosystems[eco_name].version_inputs:
                    if key in inputs:
                        raw = substitute(str(inputs[key]), context).strip().splitlines()
                        candidate = raw[-1].strip().strip("'\"") if raw else ""
                        plan.version = None if _ANY_EXPR.search(candidate) else candidate
                        break
            continue
        script = step.get("run")
        if not isinstance(script, str):
            continue
        kinds, skipped = split_script(substitute(script, context), rules.ci)
        plan.skipped += skipped
        workdir = step.get("working-directory")
        env_lines = [*job_env, *_exports(step.get("env"), context)]
        for kind, commands in kinds.items():
            if not commands:
                continue
            block = "\n".join([*env_lines, *commands])
            if isinstance(workdir, str) and not _ANY_EXPR.search(workdir):
                block = f"(cd {workdir} && {{ {block.replace(chr(10), '; ')}; }})"
            getattr(plan, kind).append(block)
    return plan if plan.test or plan.lint else None


def _triggers(doc: dict[Any, Any]) -> set[str]:
    # YAML 1.1 reads the bare key `on` as boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, str):
        return {on}
    return set(on) if isinstance(on, list | dict) else set()


def _rank(
    plan: CIPlan, job_id: str, job: dict[str, Any], triggers: set[str], rules: CIRules
) -> tuple[int, ...]:
    label = f"{job_id} {job.get('name', '')} {Path(plan.workflow).stem}".lower()
    return (
        int(bool(triggers & set(rules.contribution_events)) or not triggers),
        -sum(word in label for word in rules.specialised_job_words),
        int(job_id.lower() in rules.preferred_job_names),
        int("test" in label),
        int(plan.ecosystem is not None),
        -len(plan.skipped),
    )


def read_ci(root: Path, rules: Rules) -> CIPlan | None:
    """Return the most representative test job in the repo's CI workflows, if any.

    If that job runs no linter, lint commands are taken from the best job that does.
    """
    testing: list[tuple[tuple[int, ...], CIPlan]] = []
    linting: list[tuple[tuple[int, ...], CIPlan]] = []
    for pattern in rules.ci.workflow_globs:
        for path in sorted(root.glob(pattern)):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
            except yaml.YAMLError:
                continue
            jobs = doc.get("jobs") if isinstance(doc, dict) else None
            if not isinstance(jobs, dict):
                continue
            triggers = _triggers(doc)
            for job_id, job in jobs.items():
                if not isinstance(job, dict):
                    continue
                plan = _plan_for_job(str(path.relative_to(root)), str(job_id), job, rules)
                if plan is None:
                    continue
                rank = _rank(plan, str(job_id), job, triggers, rules.ci)
                (testing if plan.test else linting).append((rank, plan))
    if not testing:
        return None
    best = max(testing, key=lambda pair: pair[0])[1]
    if not best.lint:
        donors = [
            p
            for _, p in sorted(linting + testing, key=lambda pair: pair[0], reverse=True)
            if p.lint and p.ecosystem in (None, best.ecosystem)
        ]
        if donors:
            best.lint = donors[0].lint
    return best
