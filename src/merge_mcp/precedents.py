"""Learn what a complete contribution looks like in this repo, from its own history.

Two outputs, both computed from `git log` with no API calls and no model:

* Co-change rules: "when source files change, test files change too in 92% of commits".
  A diff that breaks a strong rule is the most common reason a first pull request gets
  sent back.
* Precedent commits: recent commits that touched the same files, as worked examples.

File kinds (tests, changelog, docs...) come from the `[categories]` rules, so this works
for any language without changes here.
"""

from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path, PurePosixPath

from .models import CoChangeRule, PrecedentCommit, PrecedentList, Precedents
from .rules import Rules
from .workspace import git

_RECORD, _FIELD = "\x1e", "\x1f"


def parse_log(log: str) -> list[tuple[str, str, list[str]]]:
    """Parse `git log --name-only --format=%x1e%H%x1f%s` into (sha, subject, files)."""
    commits = []
    for record in log.split(_RECORD):
        if not record.strip():
            continue
        head, _, rest = record.partition("\n")
        sha, _, subject = head.partition(_FIELD)
        files = [ln.strip() for ln in rest.splitlines() if ln.strip()]
        if sha and files:
            commits.append((sha.strip(), subject.strip(), files))
    return commits


def learn(commits: list[tuple[str, str, list[str]]], rules: Rules) -> Precedents | None:
    cfg = rules.precedents
    usable = [(sha, subj, files) for sha, subj, files in commits if not cfg.ignored(subj)]
    if not usable:
        return None
    kinds = [{rules.categorize(f) for f in files} for _, _, files in usable]
    triggered = [k for k in kinds if cfg.trigger in k]
    found = []
    if len(triggered) >= cfg.min_support:
        for expected in cfg.expect:
            confidence = sum(expected in k for k in triggered) / len(triggered)
            if confidence >= cfg.min_confidence:
                found.append(
                    CoChangeRule(
                        when=cfg.trigger,
                        expect=expected,
                        support=len(triggered),
                        confidence=round(confidence, 2),
                    )
                )

    # Where do changelog entries go? A fragment directory if one dominates, else the file.
    locations: Counter[str] = Counter()
    for _, _, files in usable:
        for f in files:
            if rules.categorize(f) == "changelog":
                parent = str(PurePosixPath(f).parent)
                locations[f if parent == "." else parent + "/"] += 1
    hint = locations.most_common(1)[0][0] if locations else None
    return Precedents(
        sample_size=len(usable),
        rules=found,
        changelog_hint=hint,
        median_files_changed=int(statistics.median(len(f) for _, _, f in usable)),
    )


async def mine(checkout: Path, rules: Rules) -> Precedents | None:
    log = await git(
        "log",
        "--no-merges",
        "-n",
        str(rules.precedents.history),
        "--name-only",
        f"--format={_RECORD}%H{_FIELD}%s",
        cwd=checkout,
        check=False,
    )
    return learn(parse_log(log), rules)


async def find_precedents(
    checkout: Path, paths: list[str], rules: Rules, limit: int = 5
) -> PrecedentList:
    """Recent commits that touched any of `paths` (files or directories)."""
    log = await git(
        "log",
        "--no-merges",
        "-n",
        str(limit * 3),
        "--name-only",
        f"--format={_RECORD}%H{_FIELD}%s",
        "--",
        *paths,
        cwd=checkout,
        check=False,
    )
    commits = []
    for sha, subject, _ in parse_log(log):
        if rules.precedents.ignored(subject):
            continue
        # The path-limited log lists only matching files; ask for the full file list.
        full = await git("show", "--name-only", "--format=", sha, cwd=checkout, check=False)
        files = [f for f in full.splitlines() if f.strip()]
        commits.append(
            PrecedentCommit(
                sha=sha[:12],
                subject=subject,
                files=files[:20],
                categories=sorted({rules.categorize(f) for f in files}),
            )
        )
        if len(commits) >= limit:
            break
    return PrecedentList(paths=paths, commits=commits)
