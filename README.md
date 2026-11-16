# MergeMCP

[![CI](https://github.com/acctofaditya2005/MergeMCP/actions/workflows/ci.yml/badge.svg)](https://github.com/acctofaditya2005/MergeMCP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

An MCP server that takes an open-source contribution from "which issue?" to "merged", in
any language, and a benchmark that measures how much it helps an AI agent.

A coding agent in your IDE can already edit code. It starts from nothing on every repo,
though, and most first contributions fail before or after the code: the issue was already
taken, the repo would not build, or the pull request came back for a missing test or
changelog entry. merge-mcp gives the agent that missing knowledge as tools.

| Stage | What goes wrong | What the server does |
|---|---|---|
| Pick | "Good first issue" lists are full of dead ends | Finds issues in **libraries you already depend on**, and scores each for availability: unassigned, unclaimed, no competing pull request, maintainers who merge outsiders |
| Set up | Hours lost working out how to build and test | Reads the repo's **CI workflow** for the real install, test and lint commands, in any language, and runs them in a sandbox |
| Change | The fix ignores how the project works | Shows **precedents**: recent commits that touched the same files, and what else they changed |
| Check | Review bounces it for a missing test or changelog | **Pre-flights** the diff against rules learned from the repo's own history ("93% of source changes here also changed tests") |
| Submit | Unreviewed AI pull requests | **Stops.** It drafts the pull request and prints the commands; a human reviews and submits. The server cannot push |
| Follow up | The pull request stalls on red CI or an unanswered review | Reads CI results and reviews and says what to do next |

It works in any MCP client (Claude Code, Cursor, VS Code and others).

## Quick start

Requirements: Python 3.11+, git, [uv](https://docs.astral.sh/uv/), and Docker (running).

```bash
git clone https://github.com/acctofaditya2005/MergeMCP
cd MergeMCP
uv venv && uv pip install -e .
```

Recommended: a GitHub fine-grained token with **read-only access to public
repositories**, for a higher API rate limit.

### Connect it to your client

Replace `/path/to/merge-mcp` with the clone location.

**Claude Code**

```bash
claude mcp add merge-mcp -e MERGE_MCP_GITHUB_TOKEN=github_pat_... \
  -- uv run --directory /path/to/merge-mcp merge-mcp
```

**Cursor** (`.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "merge-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/merge-mcp", "merge-mcp"],
      "env": { "MERGE_MCP_GITHUB_TOKEN": "github_pat_..." }
    }
  }
}
```

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "merge-mcp": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/merge-mcp", "merge-mcp"],
      "env": { "MERGE_MCP_GITHUB_TOKEN": "github_pat_..." }
    }
  }
}
```

Client configuration formats change; if one of these does not load, check your client's
MCP documentation for the current shape. The server speaks standard MCP over stdio.

### Use it

Ask your agent, from inside one of your own projects:

> Look at my dependency files and find an open issue I could fix in a library I use.
> Check it's really available, then set up a workspace for it.

or, for an issue you already picked:

> Help me contribute a fix for https://github.com/owner/repo/issues/123

The agent assesses the issue, clones the repo, reads the playbook, studies precedents,
makes a change, runs the project's own tests in the sandbox, pre-flights the diff, and
hands you a draft:

```text
Checklist
  [done]     Test suite passes on this exact diff
  [done]     Linter passes (`npm run lint`)
  [warning]  No changelog entry. 80% of past source changes also updated the
             changelog (usually under .changeset/).
  [todo]     Commit is signed off (DCO)
  [todo]     I have read the full diff and understand every change

Commands for you to run
  cd ~/.merge-mcp/workspaces/ws_3fa91c22d0/repo
  git diff 4be2a1c0d9e1    # review every line first
  git add -A
  git commit -s -m 'fix: handle empty input in parse()'
  gh repo fork --remote
  git push -u origin fix/issue-123-crash-on-empty-input
  gh pr create --draft --title '...' --body-file ~/.merge-mcp/workspaces/ws_3fa91c22d0/PR_DRAFT.md
```

After you open the pull request, ask the agent to check on it and it will tell you what
it needs to get merged.

## Tools

| Tool | What it does |
|---|---|
| `find_issues_in_dependencies` | Maps your dependency files to GitHub repos and finds available issues there. |
| `find_issues` | Searches all of GitHub for open, unassigned issues with no linked pull request. |
| `assess_issue` | Deep check of one issue: claims, competing pull requests, repo responsiveness. |
| `start_workspace` | Clones a repo on a new branch and builds its playbook (background job). |
| `get_job` | Polls a background job, with long-polling. |
| `get_playbook` | How to build and test the repo, its conventions, learned co-change rules, policy. |
| `find_precedents` | Recent commits that touched the same files, as worked examples. |
| `list_files`, `read_file`, `search_code` | Explore the checkout. |
| `edit_file`, `write_file` | Change files, jailed to the checkout. |
| `start_check` | Runs the repo's setup, tests or linter in the sandbox (background job). |
| `get_diff` | Everything changed since the base commit. |
| `check_readiness` | Pre-flight: what would a reviewer bounce in this diff? |
| `prepare_pull_request` | Draft title, body, checklist and the commands for the human. Submits nothing. |
| `check_pull_request` | Reads CI and reviews on an open pull request and lists next actions. |
| `list_workspaces`, `delete_workspace` | Housekeeping. |

Full parameter reference: [docs/tools.md](docs/tools.md), generated from the server's
schemas.

## Any language, without per-language code

Nothing in the server's logic names a language. Three mechanisms make that work:

1. **CI is the source of truth.** A project's CI workflow already states how to install,
   test and lint it. The server reads `.github/workflows/`, picks the everyday test job,
   collapses the matrix to one Linux combination, and splits each step into install, test
   and lint commands. Steps that need secrets, sudo or another OS are dropped and listed.
2. **Policy is data.** Languages are tables in a rules file: marker files, toolchain
   actions, sandbox image, environment, default commands. Python, Node, Go and Rust ship
   by default. Adding one means adding a table, not writing Python
   ([docs/configuration.md](docs/configuration.md)).
3. **Success is the exit code.** A test run passes if the project's own command exits
   zero, which holds for every test runner. Per-test detail is an optional parser.

When CI commands do not reproduce locally, setup falls back once to the language's default
commands and records that in the playbook. You can also override commands, or skip CI
reading, when creating a workspace.

**What has been run for real** (not mocked), using the `local` sandbox:

| Repo | Language | Commands came from | Result |
|---|---|---|---|
| [spf13/pflag](https://github.com/spf13/pflag) | Go | its CI workflow | setup and tests passed |
| [dtolnay/itoa](https://github.com/dtolnay/itoa) | Rust | its CI workflow | setup and tests passed |
| [expressjs/express](https://github.com/expressjs/express) | Node | its CI workflow | setup and tests passed |
| [pallets/click](https://github.com/pallets/click) | Python | language defaults (CI uses a tox wrapper) | setup passed, 2,627 tests passed |

CI reading is a best-effort heuristic. On a trial of ten well-known repositories it chose a
sensible job for most and fell back to defaults for the rest; elaborate pipelines (custom
actions, build artefacts passed between jobs) will not reproduce.

## Safety model

| Risk | What stops it |
|---|---|
| A repo's tests or install scripts touch your files or tokens | Docker container: only the checkout is mounted, no host environment, dropped capabilities, resource limits |
| Code phones home | No network while tests and linters run |
| An issue tells the agent to run a command | There is no shell tool; only the repo's own setup, tests and linter can run, and only in the sandbox |
| The agent reads or writes outside the repo | Path jail that resolves symlinks; `.git/` is write-protected |
| A pull request goes out unreviewed | No commit, push, fork or pull request tool; the token is read-only |

Details and known limits: [docs/security.md](docs/security.md).

## Benchmark

The benchmark exists to test the claim that these tools help. It replays real,
already-solved issues and grades an agent by running the maintainers' own tests.

```bash
# Sanity baselines (no API key): must print 100% and 0%
merge-eval run --agent oracle --out runs/oracle
merge-eval run --agent noop   --out runs/noop

# A model with the full tool set, then the same model without the server's knowledge tools
merge-eval run --agent anthropic --model <model-id> --k 3 --out runs/full
merge-eval run --agent anthropic --model <model-id> --k 3 --out runs/ablated \
    --disable-tools get_playbook,find_precedents,check_readiness

# A local model through any OpenAI-compatible endpoint (free)
merge-eval run --agent openai --model qwen2.5-coder \
    --base-url http://localhost:11434/v1 --k 3 --out runs/local
```

Each run reports Pass@k, latency, tokens, cost, tool calls and tool error rate. It also
reports acceptance-oriented measures: whether the patch passes the repo's own linter
("resolved and lint-clean"), review warnings per patch from the server's pre-flight
rules, and the share of patches that include tests.

### Results

35 tasks from attrs, boltons, cachetools, click, humanize, marshmallow, more-itertools,
structlog and tenacity, graded by 523 fail-to-pass and 6,438 pass-to-pass tests.

| Agent | Pass@1 | Harness errors | Report |
|---|---|---|---|
| `baseline:oracle` (writes the maintainers' fix through the MCP tools) | 100% (35/35) | 0 | [oracle.md](benchmarks/baselines/oracle.md) |
| `baseline:noop` (changes nothing) | 0% (0/35) | 0 | [noop.md](benchmarks/baselines/noop.md) |
| _a model, full tools_ | _not run yet_ | | |
| _the same model, ablated_ | _not run yet_ | | |

Lint is graded on the 9 tasks whose base commit passes its own linter; on those, the
oracle is 100% resolved and lint-clean and the no-op is 0%.

The baselines verify the harness, not a model. **No model has been run yet**, so the claim
that the server improves an agent's results is still a hypothesis; the two empty rows are
the experiment that tests it. The benchmark tasks are Python only for now, because grading
needs per-test results.

How tasks are mined, validated and graded: [docs/evaluation.md](docs/evaluation.md).

## Documentation

- [Architecture](docs/architecture.md): modules, state model, how a request flows.
- [Configuration and rules](docs/configuration.md): settings, the rules file, adding a language.
- [Tool reference](docs/tools.md): every tool and parameter.
- [Evaluation](docs/evaluation.md): task format, pipeline, metrics, ablations, limitations.
- [Security](docs/security.md): threat model and the human gate.
- [Design decisions](docs/design-decisions.md): each trade-off and what it costs.
- [Benchmark tasks](benchmarks/README.md): sources and provenance.
- [Contributing](CONTRIBUTING.md)

## Scope and limits

- Only GitHub, and only GitHub Actions workflows are read for commands.
- CI reading is heuristic; expect to override commands on unusual repos.
- Co-change rules need history: a repo with few commits yields none.
- The server prepares contributions; it does not judge whether a project wants them.
  Read the project's contributing guide and AI policy, which the playbook surfaces.
- Alpha software. The tool surface may change before 1.0.

## Development

```bash
uv pip install -e ".[dev]"
pytest -q                     # hermetic: no network, Docker optional
ruff check src tests scripts
```

## License

[MIT](LICENSE)
