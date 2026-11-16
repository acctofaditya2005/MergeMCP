# Security model

The server does three risky things: it runs code from repositories nobody has vetted, it
lets a model write files, and it holds a GitHub token. This page states what could go
wrong and what stops it.

## Threats and controls

| Threat | Control | Where |
|---|---|---|
| A repo's install script or test suite reads SSH keys or tokens, or deletes files | Runs in a container that mounts only the checkout. No host environment is passed in. Capabilities dropped, `no-new-privileges`, non-root user, CPU/memory/process limits. | `sandbox.DockerRunner` |
| Repo code or a model-written patch sends data out | Network is off for tests and linters. It is on only during dependency installation, which happens before the agent has edited anything in a benchmark run. | `checks.run_check` |
| An issue contains instructions aimed at the agent ("run this command") | There is no shell tool. The only things that can be executed are the repo's own install, test and lint commands, inside the sandbox. | `server.py` |
| The repo's CI file contains hostile commands | Commands read from CI are repository content and are treated as untrusted: they only ever run inside the sandbox, never on the host, and steps using `sudo`, secrets or Docker are dropped. | `ci_config.py`, `checks.py` |
| A hostile rules file | The rules file is local configuration you control, like a shell profile. It is validated, but its commands are trusted. Do not point `MERGE_MCP_RULES` at a file from a repository. | `rules.py` |
| The model reads or writes outside the workspace | Paths are resolved (following symlinks) and must stay inside the checkout. Absolute paths, `..`, `~` and NUL bytes are refused. | `workspace.resolve` |
| The model plants a git hook or edits git config | Writes under `.git/` and the virtualenv are refused. The server's own git calls run with `core.hooksPath=/dev/null`. | `workspace.resolve`, `workspace.git` |
| Crafted input becomes a git or pytest option | Refs must match a strict pattern; test ids cannot start with `-` and are shell-quoted; repo names must match `owner/name`. | `workspace.create`, `checks` |
| A pull request is opened without a human looking | No tool commits, pushes, forks or opens pull requests, and the token needs no write scope. A test asserts no such tool exists. | `handoff.py`, `tests/test_server.py` |
| The agent claims tests pass when they do not | The checklist is computed by the server from recorded results and a fingerprint of the diff, not from what the model says. | `handoff.build_draft` |
| A benchmark agent passes by editing the tests | The agent's changes to reference test files are discarded before grading. | `eval/grade.py` |
| The Docker daemon is down and failures look like test failures | The runner checks the daemon first and raises a clear error. | `DockerRunner.ensure_available` |

## The GitHub token

Use a fine-grained personal access token with **read-only access to public repositories**
and no other permission. The server only issues `GET` requests. Without a token it still
works at GitHub's lower unauthenticated rate limit.

The token is read from `MERGE_MCP_GITHUB_TOKEN` (or `GITHUB_TOKEN`). It is never passed
into the sandbox, written to a workspace, or returned by a tool.

## The human gate

`prepare_pull_request` is the last tool. It returns a draft, a checklist and the commands
to run. One checklist item is always open: "I have read the full diff and understand every
change." The human runs `git commit`, `git push` and `gh pr create --draft` themselves.

This is a structural guarantee, not a prompt instruction. A model that ignores its
instructions still cannot submit anything, because the capability does not exist and the
token could not do it anyway.

## `MERGE_MCP_SANDBOX=local`

This mode runs repository code as a plain subprocess. Only locale, proxy and certificate
variables are inherited, and any variable whose name looks like a credential is refused.
It still has **no isolation**: the code can read your files and use the network. It exists for machines
that are already disposable (a CI container, a cloud dev box). The server never falls back
to it silently; if Docker is missing you get an error that names the setting.

## Known limits

- Docker is not a perfect boundary against a determined kernel exploit. For stronger
  isolation, point Docker at gVisor (`runsc`) or run the server inside a VM.
- Dependency installation needs the network, so a malicious package could exfiltrate the
  repository contents (which are public) during setup. It cannot reach host files.
- The viability score reads public comments. Someone can game it by posting misleading
  comments; it is advice, not a guarantee.

## Reporting a vulnerability

Open a private security advisory on the GitHub repository instead of a public issue.
