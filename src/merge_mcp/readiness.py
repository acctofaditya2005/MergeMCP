"""Pre-flight: would a reviewer send this diff back?

`assess` compares the current diff with what the repo's own history and contributor docs
say a complete change contains. It is a pure function of its inputs, so the same diff and
playbook always produce the same findings, and it runs in milliseconds.
"""

from __future__ import annotations

from .models import Finding, Playbook, Readiness
from .rules import Rules

_EXPECT_MESSAGES = {
    "tests": "No test files changed. {pct} of past source changes in this repo also changed tests.",
    "changelog": "No changelog entry. {pct} of past source changes also updated the changelog"
    "{hint}.",
    "docs": "No documentation changed. {pct} of past source changes also touched docs.",
}


def assess(
    files_changed: list[str],
    playbook: Playbook,
    rules: Rules,
    *,
    last_checks: dict[str, dict],
    fingerprint: str,
) -> Readiness:
    by_category: dict[str, list[str]] = {}
    for path in files_changed:
        by_category.setdefault(rules.categorize(path), []).append(path)
    findings: list[Finding] = []

    def add(code: str, severity: str, message: str, evidence: str | None = None) -> None:
        findings.append(
            Finding(
                code=code,
                severity=severity,
                message=message,  # type: ignore[arg-type]
                evidence=evidence,
            )
        )

    if not files_changed:
        add("empty-diff", "blocker", "The workspace has no changes.")
        return Readiness(ready=False, files_by_category={}, findings=findings)

    # 1. Did the repo's own checks pass on this exact diff?
    for kind, label, required in (
        ("test", "Tests", True),
        ("lint", "Linter", bool(playbook.tooling.lint_command)),
    ):
        if not required:
            continue
        record = last_checks.get(kind)
        if record is None:
            add(f"{kind}-not-run", "warning", f"{label} have not been run on this diff.")
        elif record.get("fingerprint") != fingerprint:
            add(
                f"{kind}-stale",
                "warning",
                f"{label} last ran on an older version of the diff. Run them again.",
            )
        elif not record.get("ok"):
            add(f"{kind}-failing", "blocker", f"{label} failed on this diff.")
        elif record.get("partial"):
            add(f"{kind}-partial", "warning", f"{label}: only a subset was run last time.")

    # 2. Does the diff look like changes this repo has merged before?
    precedents = playbook.precedents
    cfg = rules.precedents
    if precedents and cfg.trigger in by_category:
        for rule in precedents.rules:
            if rule.expect in by_category:
                continue
            hint = (
                f" (usually under {precedents.changelog_hint})"
                if rule.expect == "changelog" and precedents.changelog_hint
                else ""
            )
            template = _EXPECT_MESSAGES.get(
                rule.expect, "No {expect} files changed. {pct} of past source changes did."
            )
            add(
                f"missing-{rule.expect}",
                "warning",
                template.format(pct=f"{rule.confidence:.0%}", hint=hint, expect=rule.expect),
                evidence=f"{rule.support} commits sampled",
            )
    if precedents and precedents.median_files_changed:
        limit = max(3, int(precedents.median_files_changed * cfg.large_change_factor))
        if len(files_changed) > limit:
            add(
                "large-change",
                "warning",
                f"This diff touches {len(files_changed)} files; the typical change here "
                f"touches {precedents.median_files_changed}. Consider splitting it.",
            )
    if by_category.get("dependencies") and cfg.trigger in by_category:
        add(
            "lockfile-changed",
            "warning",
            "Dependency or lock files changed alongside source. Revert them unless the fix "
            "needs a dependency change.",
            evidence=", ".join(by_category["dependencies"][:3]),
        )
    if by_category.get("ci"):
        add(
            "ci-changed",
            "warning",
            "CI configuration changed. Maintainers review this closely; "
            "keep it out of a bug-fix pull request.",
            evidence=", ".join(by_category["ci"][:3]),
        )

    # 3. Process requirements only a human can satisfy.
    policy = playbook.policy
    if policy.requires_dco_signoff:
        add("dco", "info", "Commits must be signed off: use `git commit -s`.")
    if policy.requires_cla:
        add("cla", "info", "A Contributor License Agreement must be signed by you.")
    if policy.ai_policy_mentions:
        add(
            "ai-policy",
            "info",
            "The project has rules on AI-assisted contributions.",
            evidence=" | ".join(policy.ai_policy_mentions[:2]),
        )
    if playbook.tooling.uses_pre_commit:
        add("pre-commit", "info", "The repo uses pre-commit: run `pre-commit run --all-files`.")

    ready = not any(f.severity in ("blocker", "warning") for f in findings)
    return Readiness(ready=ready, files_by_category=by_category, findings=findings)
