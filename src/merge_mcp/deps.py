"""Find issues in the libraries a person already uses.

The best first contribution is usually to a dependency: you already know how the library
behaves from the outside. This module turns dependency manifests into GitHub repos and
searches those repos for available issues.

Per-ecosystem knowledge (which file is a manifest, which registry to ask, where in the
registry's JSON the repository URL lives) is data in `[ecosystems.*]`. The only code per
format is a small manifest parser, registered by name in `MANIFEST_PARSERS`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import httpx

from .discovery import rank_issues
from .github import GitHubClient
from .models import DependencyIssueResult, DependencyRepo
from .rules import Rules

_GITHUB = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:[/#?]|$)")
_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _requirements(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        if m := _REQ_NAME.match(line):
            names.append(m.group(1))
    return names


def _pyproject(text: str) -> list[str]:
    data = tomllib.loads(text)
    project = data.get("project", {})
    specs = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        specs += group
    for group in data.get("dependency-groups", {}).values():
        specs += [s for s in group if isinstance(s, str)]
    poetry = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    names = [n for n in poetry if n.lower() != "python"]
    return names + [m.group(1) for s in specs if (m := _REQ_NAME.match(s))]


def _package_json(text: str) -> list[str]:
    data = json.loads(text)
    return [name for key in ("dependencies", "devDependencies") for name in (data.get(key) or {})]


def _go_mod(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.split("//", 1)[0].strip().removeprefix("require").strip(" (")
        parts = line.split()
        if len(parts) >= 2 and "." in parts[0] and "/" in parts[0] and parts[1].startswith("v"):
            names.append(parts[0])
    return names


def _cargo_toml(text: str) -> list[str]:
    data = tomllib.loads(text)
    return [name for key in ("dependencies", "dev-dependencies") for name in data.get(key, {})]


#: Named manifest parsers an ecosystem rule can reference in its `manifests` table.
MANIFEST_PARSERS: dict[str, Callable[[str], list[str]]] = {
    "requirements": _requirements,
    "pyproject": _pyproject,
    "package_json": _package_json,
    "go_mod": _go_mod,
    "cargo_toml": _cargo_toml,
}


def parse_manifest(filename: str, content: str, rules: Rules) -> list[tuple[str, str]]:
    """Return (ecosystem, package name) pairs from one manifest file."""
    base = PurePosixPath(filename).name
    for eco_name, eco in rules.ecosystems.items():
        for pattern, parser in eco.manifests.items():
            if fnmatch.fnmatch(base, pattern):
                try:
                    names = MANIFEST_PARSERS[parser](content)
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"Could not parse {filename}: {exc}") from exc
                return [(eco_name, n) for n in dict.fromkeys(names)]
    known = sorted(p for e in rules.ecosystems.values() for p in e.manifests)
    raise ValueError(f"Unrecognised manifest {filename!r}. Supported: {', '.join(known)}")


def _dig(data: Any, dotted: str) -> Any:
    for key in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def github_repo_from(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("url")
    if isinstance(value, str) and (m := _GITHUB.search(value)):
        return f"{m.group(1)}/{m.group(2)}"
    return None


async def resolve_repo(
    http: httpx.AsyncClient, ecosystem: str, package: str, rules: Rules
) -> str | None:
    """Map a package to 'owner/name' using its registry's metadata."""
    if direct := github_repo_from(package):  # Go module paths are the repo
        return direct
    eco = rules.ecosystems[ecosystem]
    if not eco.registry_url:
        return None
    try:
        resp = await http.get(eco.registry_url.format(name=package))
        if resp.status_code != 200:
            return None
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    for field in eco.repo_fields:
        if repo := github_repo_from(_dig(data, field)):
            return repo
    return None


async def find_issues_in_dependencies(
    gh: GitHubClient,
    http: httpx.AsyncClient,
    rules: Rules,
    packages: list[tuple[str, str]],
    *,
    labels: list[str] | None = None,
    max_repos: int = 15,
    limit: int = 10,
    now: datetime | None = None,
) -> DependencyIssueResult:
    now = now or datetime.now(UTC)
    unique = list(dict.fromkeys(packages))
    gate = asyncio.Semaphore(6)

    async def resolve(eco: str, name: str) -> DependencyRepo:
        async with gate:
            return DependencyRepo(
                package=name, ecosystem=eco, repo=await resolve_repo(http, eco, name, rules)
            )

    resolved = await asyncio.gather(*(resolve(e, n) for e, n in unique[:60]))
    repos = list(dict.fromkeys(r.repo for r in resolved if r.repo))[:max_repos]
    label_filter = ",".join(f'"{lb}"' for lb in (labels or rules.viability.search_labels))

    queries, items = [], []
    for start in range(0, len(repos), 5):  # a few repos per query keeps it under length limits
        scope = " ".join(f"repo:{r}" for r in repos[start : start + 5])
        query = f"is:issue is:open no:assignee -linked:pr label:{label_filter} {scope}"
        queries.append(query)
        data = await gh.search_issues(query, per_page=30)
        items += [i for i in data.get("items", []) if "pull_request" not in i]

    candidates, note = await rank_issues(gh, items, rules.viability, now)
    return DependencyIssueResult(
        packages_seen=len(unique),
        resolved=[r for r in resolved if r.repo],
        unresolved=[r.package for r in resolved if not r.repo],
        queries=queries,
        candidates=candidates[:limit],
        note=note,
    )
