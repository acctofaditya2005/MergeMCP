# Contributing

Thanks for helping. This project is itself a tool for contributing well, so the bar is
the same one it asks of others: small, tested, explained changes.

## Setup

```bash
git clone https://github.com/acctofaditya2005/MergeMCP
cd MergeMCP
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Before you open a pull request

```bash
ruff check src tests scripts
ruff format --check src tests scripts
pytest -q
python scripts/gen_tool_docs.py --check   # tool reference matches the server
```

The test suite is hermetic: it builds a small git repository on the fly and needs neither
network access nor Docker. One test exercises the real Docker sandbox and is skipped when
no daemon is running.

## Ground rules

- **Tools stay thin.** Logic goes in a service module with unit tests; `server.py` only
  adapts it to MCP.
- **Every tool returns a Pydantic model** from `models.py`. No ad-hoc dicts.
- **No tool may commit, push, fork, or run arbitrary commands.** `tests/test_server.py`
  enforces this. If you think you need one, open an issue first.
- **Scoring and playbook rules stay deterministic.** No LLM calls inside the server.
- **No language names in the logic.** If a change needs `if ecosystem == "..."`, it belongs
  in `defaults.toml` instead. See [docs/configuration.md](docs/configuration.md).
- **Opinions go in the rules file.** A new weight, threshold or pattern is a new key in
  `defaults.toml` with a matching field in `rules.py`, not a constant.
- **No language names in the logic.** If a change needs `if ecosystem == "..."`, it belongs
  in `defaults.toml` instead. See [docs/configuration.md](docs/configuration.md).
- **Opinions go in the rules file.** A new weight, threshold or pattern is a new key in
  `defaults.toml` with a matching field in `rules.py`, not a constant.
- If you change a tool's signature or description, regenerate `docs/tools.md` with
  `python scripts/gen_tool_docs.py`.
- If you change the harness or add tasks, run the `oracle` and `noop` baselines and
  confirm they score 100% and 0%.

## Adding a language

Add an `[ecosystems.<name>]` table to `src/merge_mcp/defaults.toml` and, if its test
commands are not already recognised, patterns under `[ci.kinds]`. Then add a fixture to
`tests/test_multilang.py`: a few files and a CI workflow. That test drives the real
toolchain and is skipped where it is not installed.

## Adding a language

Add an `[ecosystems.<name>]` table to `src/merge_mcp/defaults.toml` and, if its test
commands are not already recognised, patterns under `[ci.kinds]`. Then add a fixture to
`tests/test_multilang.py`: a few files and a CI workflow. That test drives the real
toolchain and is skipped where it is not installed.

## Adding benchmark tasks

```bash
merge-eval mine owner/repo --out benchmarks/candidates
merge-eval validate benchmarks/candidates --out benchmarks/tasks
merge-eval run --agent oracle --out runs/oracle
```

Only commit tasks that validate. Prefer repositories with permissive licenses and a fast,
self-contained pytest suite.

## AI-assisted contributions

Welcome, on the same terms this tool proposes to other projects: say so in the pull
request, and review every line yourself before submitting.

## License

By contributing you agree that your contributions are licensed under the MIT License.
