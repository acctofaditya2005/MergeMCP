"""Rules: the tunable policy of the server, loaded from data.

`defaults.toml` ships with the package. A user file (MERGE_MCP_RULES, or
`<home>/rules.toml`) is deep-merged over it, so changing a scoring weight or adding a
language ecosystem needs no code change. Pydantic validates the merged result at start-up,
so a typo in a rules file fails loudly instead of silently changing behaviour.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections import Counter
from functools import cached_property
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, ignored_types=(cached_property,))


class ViabilityRules(_Strict):
    good_at: int
    risky_at: int
    claim_window_days: int
    stale_issue_days: int
    inactive_repo_days: int
    slow_merge_days: int
    newcomer_labels: list[str]
    search_labels: list[str]
    internal_associations: list[str]
    claim_pattern: str
    weights: dict[str, int]

    @cached_property
    def claim_regex(self) -> re.Pattern[str]:
        return re.compile(self.claim_pattern, re.IGNORECASE)


class Ecosystem(_Strict):
    markers: list[str]
    extensions: list[str] = Field(default_factory=list)
    setup_actions: list[str] = Field(default_factory=list)
    version_inputs: list[str] = Field(default_factory=list)
    version_pattern: str = r"^\d+(\.\d+)*$"
    default_version: str
    image: str
    env: dict[str, str] = Field(default_factory=dict)
    env_setup: str | None = None
    install: str
    test: str
    lint: str | None = None
    detector: str | None = None
    result_parser: str | None = None
    test_id_runners: list[str] = Field(default_factory=list)
    ci_test_must_match: list[str] = Field(default_factory=list)
    test_flags: str = ""
    exclude: list[str] = Field(default_factory=list)
    manifests: dict[str, str] = Field(default_factory=dict)
    registry_url: str | None = None
    repo_fields: list[str] = Field(default_factory=list)

    def image_for(self, version: str | None) -> str:
        ok = version is not None and re.fullmatch(self.version_pattern, version)
        return self.image.format(version=version if ok else self.default_version)

    def trusts_ci_test(self, command: str) -> bool:
        if not self.ci_test_must_match:
            return True
        return all(
            any(re.search(p, line) for p in self.ci_test_must_match)
            for line in command.splitlines()
            if line.strip() and not line.startswith(("export ", "cd "))
        )

    def accepts_test_ids(self, command: str) -> bool:
        return any(re.search(p, command) for p in self.test_id_runners)


class CIRules(_Strict):
    workflow_globs: list[str]
    skip_runners: list[str]
    contribution_events: list[str]
    preferred_job_names: list[str]
    specialised_job_words: list[str]
    prefer_matrix: list[str]
    unstable_versions: list[str]
    skip_step_patterns: list[str]
    kinds: dict[str, list[str]]

    def classify(self, script: str) -> str | None:
        """Return 'test', 'lint' or 'install' for a CI step's script, or None."""
        for kind in ("test", "lint", "install"):
            if any(re.search(p, script, re.MULTILINE) for p in self.kinds.get(kind, [])):
                return kind
        return None

    def reproducible(self, script: str) -> bool:
        return not any(re.search(p, script) for p in self.skip_step_patterns)


class PrecedentRules(_Strict):
    history: int
    trigger: str
    expect: list[str]
    min_support: int
    min_confidence: float
    large_change_factor: float
    ignore_subject_patterns: list[str]

    def ignored(self, subject: str) -> bool:
        return any(re.search(p, subject) for p in self.ignore_subject_patterns)


class PolicyRules(_Strict):
    contributing_paths: list[str]
    pr_template_paths: list[str]
    code_of_conduct_paths: list[str]
    extra_policy_paths: list[str]
    dco_pattern: str
    cla_pattern: str
    ai_pattern: str


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """`*` stays inside one path segment, `**` crosses segments."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(out + r"\Z")


class Rules(_Strict):
    viability: ViabilityRules
    ecosystems: dict[str, Ecosystem]
    ci: CIRules
    categories: dict[str, list[str]]
    precedents: PrecedentRules
    policy: PolicyRules

    @cached_property
    def _category_regexes(self) -> list[tuple[str, list[re.Pattern[str]]]]:
        return [
            (name, [glob_to_regex(g) for g in globs]) for name, globs in self.categories.items()
        ]

    def categorize(self, path: str) -> str:
        """Which kind of file is this? Unmatched paths are 'source'."""
        for name, regexes in self._category_regexes:
            if any(r.match(path) for r in regexes):
                return name
        return "source"

    def ecosystem_for_action(self, uses: str) -> str | None:
        action = uses.split("@", 1)[0].lower()
        for name, eco in self.ecosystems.items():
            if any(action.startswith(a.lower()) for a in eco.setup_actions):
                return name
        return None

    def ecosystem_for_repo(self, root: Path, sample: int = 3000) -> str | None:
        """Identify the ecosystem: a marker file at the root, else the dominant source
        file extension among the first `sample` files."""
        for name, eco in self.ecosystems.items():
            if any((root / marker).is_file() for marker in eco.markers):
                return name
        by_extension = {
            ext: name for name, eco in self.ecosystems.items() for ext in eco.extensions
        }
        counts: Counter[str] = Counter()
        seen = 0
        skip = {e.strip("/") for eco in self.ecosystems.values() for e in eco.exclude}
        for _directory, subdirs, files in os.walk(root):
            subdirs[:] = [d for d in subdirs if not d.startswith(".") and d not in skip]
            for filename in files:
                seen += 1
                if owner := by_extension.get(Path(filename).suffix):
                    counts[owner] += 1
            if seen >= sample:
                break
        return counts.most_common(1)[0][0] if counts else None


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Tables merge key by key; any other value in `override` replaces the base value."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_rules(*overrides: Path | None) -> Rules:
    text = resources.files("merge_mcp").joinpath("defaults.toml").read_text(encoding="utf-8")
    data = tomllib.loads(text)
    for path in overrides:
        if path is not None and path.is_file():
            data = deep_merge(data, tomllib.loads(path.read_text(encoding="utf-8")))
    return Rules.model_validate(data)
