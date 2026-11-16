"""Runtime settings, read once from the environment.

Every knob lives here so the rest of the code takes a `Settings` object instead of
reaching into `os.environ`. That keeps tests hermetic: they build their own Settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SandboxKind = Literal["docker", "local"]


def _default_home() -> Path:
    return Path(os.environ.get("MERGE_MCP_HOME", Path.home() / ".merge-mcp"))


@dataclass(frozen=True)
class Settings:
    home: Path = field(default_factory=_default_home)
    #: Read-only GitHub token. Optional: unauthenticated calls work at a lower rate limit.
    github_token: str | None = None
    github_api: str = "https://api.github.com"
    sandbox: SandboxKind = "docker"
    #: Fallback image, used only when no ecosystem rule matches the repository.
    sandbox_image: str = "python:3.12"
    #: Optional TOML file merged over the built-in rules (see defaults.toml).
    rules_path: Path | None = None
    sandbox_cpus: float = 2.0
    sandbox_memory: str = "2g"
    setup_timeout_s: int = 900
    check_timeout_s: int = 600
    #: History fetched per workspace. Conventions and co-change rules are learned from it.
    clone_depth: int = 200
    #: Allow workspaces from local paths / file:// URLs. Off for the server, on for evals.
    allow_local_sources: bool = False
    #: Memory guard only. Results are parsed from the full output, so this must stay far
    #: above the size of a verbose test run; what a tool returns is trimmed separately.
    max_output_chars: int = 5_000_000

    @property
    def workspaces_dir(self) -> Path:
        return self.home / "workspaces"

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        sandbox = env.get("MERGE_MCP_SANDBOX", "docker")
        if sandbox not in ("docker", "local"):
            raise ValueError(f"MERGE_MCP_SANDBOX must be 'docker' or 'local', got {sandbox!r}")
        return cls(
            github_token=env.get("MERGE_MCP_GITHUB_TOKEN") or env.get("GITHUB_TOKEN") or None,
            sandbox=sandbox,  # type: ignore[arg-type]
            sandbox_image=env.get("MERGE_MCP_SANDBOX_IMAGE", "python:3.12"),
            rules_path=Path(env["MERGE_MCP_RULES"]) if env.get("MERGE_MCP_RULES") else None,
            clone_depth=int(env.get("MERGE_MCP_CLONE_DEPTH", "200")),
            sandbox_cpus=float(env.get("MERGE_MCP_SANDBOX_CPUS", "2")),
            sandbox_memory=env.get("MERGE_MCP_SANDBOX_MEMORY", "2g"),
            setup_timeout_s=int(env.get("MERGE_MCP_SETUP_TIMEOUT", "900")),
            check_timeout_s=int(env.get("MERGE_MCP_CHECK_TIMEOUT", "600")),
            allow_local_sources=env.get("MERGE_MCP_ALLOW_LOCAL_SOURCES", "") == "1",
        )
