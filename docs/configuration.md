# Configuration and rules

There are two layers. **Settings** are per-machine switches read from environment
variables. **Rules** are the server's policy (scoring weights, language ecosystems, how
CI steps are recognised, what counts as a test file) and live in a data file, so they can
be changed or extended without touching code.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `MERGE_MCP_GITHUB_TOKEN` | none | Read-only GitHub token. Falls back to `GITHUB_TOKEN`. |
| `MERGE_MCP_HOME` | `~/.merge-mcp` | Where workspaces and job results are stored. |
| `MERGE_MCP_RULES` | none | Path to a rules file merged over the defaults. |
| `MERGE_MCP_SANDBOX` | `docker` | `docker`, or `local` for no isolation (disposable machines only). |
| `MERGE_MCP_SANDBOX_IMAGE` | `python:3.12` | Fallback image, used only when no ecosystem matches. |
| `MERGE_MCP_SANDBOX_CPUS` / `_MEMORY` | `2` / `2g` | Container limits. |
| `MERGE_MCP_SETUP_TIMEOUT` / `MERGE_MCP_CHECK_TIMEOUT` | `900` / `600` | Seconds. |
| `MERGE_MCP_CLONE_DEPTH` | `200` | Commits fetched per workspace; conventions are learned from them. |

## Rules

The defaults are in [`src/merge_mcp/defaults.toml`](../src/merge_mcp/defaults.toml),
which is commented and is the reference for every key. To change something, put only the
tables you want to change in your own file:

- `~/.merge-mcp/rules.toml` is loaded automatically if it exists, then
- the file named by `MERGE_MCP_RULES`, which wins.

Merging is table by table: a key you set replaces the default, keys you leave out keep
their defaults, and lists are replaced whole. The merged result is validated at start-up,
so a misspelled key stops the server with a message that names it.

### Example: stricter scoring

```toml
[viability]
good_at = 80

[viability.weights]
assigned = -100        # never suggest an assigned issue
slow_merges = -30
```

### Example: add a language

No Python is needed. This teaches the server Elixir:

```toml
[ecosystems.elixir]
markers = ["mix.exs"]
extensions = [".ex", ".exs"]
setup_actions = ["erlef/setup-beam"]
version_inputs = ["elixir-version"]
default_version = "1.17"
image = "elixir:{version}"
install = "mix local.hex --force && mix deps.get"
test = "mix test"
lint = "mix format --check-formatted"
exclude = ["_build/", "deps/"]

[ecosystems.elixir.env]
MIX_HOME = "$PWD/.merge-mcp-cache/mix"
HEX_HOME = "$PWD/.merge-mcp-cache/hex"
```

With that, a repo containing `mix.exs` gets the right sandbox image and default commands,
and a CI workflow that uses `erlef/setup-beam` is recognised as Elixir.

To have the server adopt the repo's own CI test command (say `mix test --cover`) instead
of the default, the command must also be recognisable as a test step. Lists are replaced
whole when merged, so copy the default `[ci.kinds] test` list from `defaults.toml` and add
your pattern to it:

```toml
[ci.kinds]
test = [
  # ...the default patterns from defaults.toml...
  '\bmix\s+test\b',
]
```

### What each section controls

| Section | Controls |
|---|---|
| `[viability]` | Thresholds, label lists, the "someone claimed this" pattern, and the weight of every scoring rule. |
| `[ecosystems.<name>]` | One language: how to recognise it, its sandbox image, environment, default commands, dependency manifests and package registry. |
| `[ci]` | Which workflow files to read, which jobs to prefer, which steps cannot run locally, and the patterns that classify a step as install, test or lint. |
| `[categories]` | Globs that sort changed files into tests, changelog, docs, CI and dependency files. Used to learn co-change rules and to pre-flight a diff. |
| `[precedents]` | How much history to sample and how strong a pattern must be before it is reported. |
| `[policy]` | Where to look for contributor docs and the patterns for DCO, CLA and AI-policy mentions. |

### What still needs code

Three things are registered by name in Python and referenced from the rules file:

- **Detectors** (`conventions.DETECTORS`): optional refinement of an ecosystem's default
  commands from its packaging files. Only Python has one.
- **Result parsers** (`checks.PARSERS`): turn test output into per-test results. Only
  pytest has one; every other runner is judged by its exit code, which is enough for
  everything except the benchmark's per-test grading.
- **Manifest parsers** (`deps.MANIFEST_PARSERS`): read a dependency file format.

Each is a small function added to a dictionary.
