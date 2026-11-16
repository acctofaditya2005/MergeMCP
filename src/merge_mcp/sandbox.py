"""Where untrusted code runs.

A repo's install scripts and test suite are code written by strangers, and in the
benchmark the patches are written by a model. Both run behind the `Runner` interface:

* `DockerRunner` (default): one throwaway container per command, no host credentials,
  dropped capabilities, CPU/memory/process limits, and no network unless the step is
  dependency installation.
* `LocalRunner` (opt-in, `MERGE_MCP_SANDBOX=local`): a plain subprocess with a scrubbed
  environment. It gives NO isolation. It exists for CI containers and other machines
  that are already disposable.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import Settings

VENV_DIR = ".merge-mcp-venv"
#: Server-owned scratch space inside the checkout: caches and the "setup finished" marker.
STATE_DIR = ".merge-mcp-cache"
SETUP_MARKER = f"{STATE_DIR}/setup-ok"

#: Name patterns a LocalRunner child may inherit: locale, proxy and certificate settings
#: that package managers need to reach their registries. Nothing else is passed through.
_LOCAL_ENV_PATTERNS = (
    "PATH",
    "LANG",
    "LC_*",
    "TZ",
    "*_PROXY",
    "*_proxy",
    "SSL_CERT_*",
    "*CA_BUNDLE",
    "*_CAINFO",
    "NODE_EXTRA_CA_CERTS",
    "PIP_CERT",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
)
#: A second line of defence: a variable whose name looks like a credential is never
#: inherited, whatever pattern it matches.
_SECRET_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")


def inheritable(name: str) -> bool:
    if any(word in name.upper() for word in _SECRET_WORDS):
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in _LOCAL_ENV_PATTERNS)


class SandboxUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecResult:
    command: str
    exit_code: int
    output: str
    duration_s: float
    timed_out: bool
    sandbox: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Runner(Protocol):
    name: str

    async def run(
        self,
        workdir: Path,
        command: str,
        *,
        timeout_s: int,
        network: bool = False,
        image: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult: ...


def wrap_command(command: str, env: dict[str, str] | None = None) -> str:
    """Prefix a command with the ecosystem's environment and stop at the first failure.

    The variables come from the `[ecosystems.<name>.env]` rules, for example putting a
    virtualenv first on PATH or keeping a module cache inside the checkout. Values may
    reference `$PWD` and other variables, so they are double-quoted, not single-quoted.
    """
    exports = " ".join(
        f'{key}="{value.replace(chr(34), chr(92) + chr(34))}"' for key, value in (env or {}).items()
    )
    prefix = f"export {exports}; " if exports else ""
    return f"set -e; {prefix}{command}"


def tail(text: str, limit: int) -> str:
    """Keep the end of long output: failures and summaries are printed last."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} characters truncated ...]\n" + text[-limit:]


async def _communicate(proc: asyncio.subprocess.Process, timeout_s: int) -> tuple[str, bool]:
    """Collect output until the process ends or the deadline passes.

    Output is read in chunks so that a timeout still returns what was printed so far;
    the last lines before a hang are usually the only clue to its cause.
    """
    assert proc.stdout is not None
    chunks: list[bytes] = []

    async def pump() -> None:
        while chunk := await proc.stdout.read(65536):
            chunks.append(chunk)
        await proc.wait()

    timed_out = False
    try:
        await asyncio.wait_for(pump(), timeout=timeout_s)
    except TimeoutError:
        timed_out = True
    text = b"".join(chunks).decode("utf-8", "replace")
    if timed_out:
        text += f"\n[timed out after {timeout_s}s]"
    return text, timed_out


class DockerRunner:
    name = "docker"

    def __init__(self, settings: Settings):
        self.settings = settings
        self._daemon_ok = False

    async def ensure_available(self) -> None:
        """Fail loudly if Docker cannot run containers.

        Without this check an unreachable daemon looks like a failing test run (both exit
        non-zero), and the caller would silently record wrong results.
        """
        if self._daemon_ok:
            return
        if shutil.which("docker") is None:
            raise SandboxUnavailable(
                "Docker is not installed. Install Docker, or set MERGE_MCP_SANDBOX=local to run "
                "repository code directly on this machine with no isolation."
            )
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "version",
            "--format",
            "{{.Server.Version}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            detail = out.decode("utf-8", "replace").strip()[-300:]
            raise SandboxUnavailable(f"Docker is installed but not usable: {detail}")
        self._daemon_ok = True

    def build_argv(
        self,
        workdir: Path,
        command: str,
        *,
        network: bool,
        name: str,
        image: str | None = None,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """The exact `docker run` invocation. Kept separate so tests can assert on it."""
        s = self.settings
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "bridge" if network else "none",
            "--cpus",
            str(s.sandbox_cpus),
            "--memory",
            s.sandbox_memory,
            "--pids-limit",
            "512",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--volume",
            f"{workdir}:/work",
            "--workdir",
            "/work",
            "--env",
            "HOME=/tmp",
        ]
        if hasattr(os, "getuid"):
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        return [*argv, image or s.sandbox_image, "bash", "-c", wrap_command(command, env)]

    async def run(
        self,
        workdir: Path,
        command: str,
        *,
        timeout_s: int,
        network: bool = False,
        image: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        await self.ensure_available()
        name = f"merge-mcp-{uuid.uuid4().hex[:12]}"
        argv = self.build_argv(workdir, command, network=network, name=name, image=image, env=env)
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        output, timed_out = await _communicate(proc, timeout_s)
        if timed_out:
            killer = await asyncio.create_subprocess_exec(
                "docker",
                "kill",
                name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        code = proc.returncode if proc.returncode is not None else -1
        if code == 125:  # `docker run` itself failed: daemon down, image missing, bad flag
            raise SandboxUnavailable(f"Docker could not start the sandbox: {output.strip()[-400:]}")
        return ExecResult(
            command=command,
            exit_code=code,
            output=tail(output, self.settings.max_output_chars),
            duration_s=round(time.monotonic() - start, 2),
            timed_out=timed_out,
            sandbox=self.name,
        )


class LocalRunner:
    name = "local-unsandboxed"

    def __init__(self, settings: Settings):
        self.settings = settings

    def build_env(self, workdir: Path) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if inheritable(k)}
        env["HOME"] = str(workdir.parent / "home")
        # HOME is redirected so repo code cannot read dotfiles. Rules may use this to find
        # a toolchain that lives under the real home (see [ecosystems.rust.env]).
        env["MERGE_MCP_HOST_HOME"] = str(Path.home())
        Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
        return env

    async def run(
        self,
        workdir: Path,
        command: str,
        *,
        timeout_s: int,
        network: bool = False,
        image: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        start = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            wrap_command(command, env),
            cwd=workdir,
            env=self.build_env(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        output, timed_out = await _communicate(proc, timeout_s)
        if timed_out:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
        code = proc.returncode if proc.returncode is not None else -1
        return ExecResult(
            command=command,
            exit_code=code,
            output=tail(output, self.settings.max_output_chars),
            duration_s=round(time.monotonic() - start, 2),
            timed_out=timed_out,
            sandbox=self.name,
        )


def make_runner(settings: Settings) -> Runner:
    return LocalRunner(settings) if settings.sandbox == "local" else DockerRunner(settings)
