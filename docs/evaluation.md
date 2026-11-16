# Evaluation

The benchmark answers one question: **given a real issue, can an agent using this server
produce a fix that passes the maintainers' own tests, and at what cost?**

Nothing is graded by an LLM or by text similarity. A task is resolved only when real tests
pass against the agent's code.

## Task format

Each task is one JSON file in `benchmarks/tasks/`, built from an issue that maintainers
already fixed:

| Field | Meaning |
|---|---|
| `repo`, `base_commit` | The repository state just before the fix. |
| `problem_statement` | The issue title and body. This is all the agent is told. |
| `gold_patch` | The maintainers' source change. Hidden from the agent. |
| `test_patch`, `test_files` | The tests the maintainers added or changed. Hidden from the agent. |
| `fail_to_pass` | Tests that fail before the fix and pass after it. All must pass. |
| `pass_to_pass` | Tests that pass before and after. All must still pass (no regressions). |
| `lint_baseline` | The repo's own linter on the untouched base commit: `clean`, `dirty`, or `none` (no linter, or it cannot run). |
| `validated` | True only after the validator has executed the task. |

This is the same grading idea as SWE-bench, applied to a small set of repositories and to
an agent that works only through this server's tools.

## Pipeline

```
mine ──► candidates ──► validate ──► tasks ──► run ──► attempts.jsonl ──► report
```

### 1. Mine

```bash
merge-eval mine pallets/click --out benchmarks/candidates --limit 10
```

Walks the repo's git history for commits that reference a closed issue, change at least
one test file and at most three Python source files, and touch no dependency or config
file. The commit's parent becomes `base_commit`; its diff is split into `test_patch` and
`gold_patch`. Diffs come from git, so mining uses almost no API quota.

### 2. Validate

```bash
merge-eval validate benchmarks/candidates --out benchmarks/tasks
```

A mined candidate is a guess. Validation executes it:

1. Check out `base_commit`, install the project, apply `test_patch`.
2. Run the reference tests: record what passes **before** the fix.
3. Apply `gold_patch` and run them again, twice.
4. `fail_to_pass` = passes after (on both runs) but not before.
   `pass_to_pass` = passes before and after (on both runs).

The candidate is rejected if the patch does not apply, the environment does not install,
the tests time out, or the fix turns no failing test green. Requiring identical results on
repeated runs filters out flaky tests, and the runs are spaced 1.5 seconds apart so tests
whose ids contain the current time are dropped too. `run` refuses unvalidated tasks.

On the first mining pass (42 candidates from 9 repositories), validation kept 35 and
rejected 7.

### 3. Run

```bash
# Baselines: no API key needed
merge-eval run --agent oracle --out runs/oracle
merge-eval run --agent noop   --out runs/noop

# A hosted model (set ANTHROPIC_API_KEY)
merge-eval run --agent anthropic --model <model-id> --k 3 \
    --price-in <usd-per-MTok> --price-out <usd-per-MTok> --out runs/my-model

# Any OpenAI-compatible endpoint, including a local model
merge-eval run --agent openai --model qwen2.5-coder \
    --base-url http://localhost:11434/v1 --k 3 --out runs/local
```

For each task and attempt the harness:

1. Creates a fresh workspace at `base_commit` and installs dependencies.
2. Hands the agent the issue text and eleven server tools (playbook, precedents, read,
   search, edit, run checks, pre-flight).
   `workspace_id` is removed from the tool schemas and injected by the harness, so the
   agent cannot address another workspace.
3. Lets the agent work until it calls `submit`, stops calling tools, or hits `--max-steps`.
4. Discards the agent's changes to the reference test files, applies `test_patch`, and
   runs the reference tests in the sandbox.
5. Records an `Attempt` and deletes the workspace.

### 4. Report

Each run directory contains:

- `attempts.jsonl`: one record per attempt (resolved, tokens, latency, steps, errors).
- `summary.json` and `report.md`: the aggregated metrics.
- `artifacts/<task>/attempt-N/`: the agent's patch and full transcript, for debugging.

## Metrics

| Metric | Definition |
|---|---|
| **Pass@k** | Probability that at least one of k attempts resolves a task, averaged over tasks. Uses the unbiased estimator `1 − C(n−c, k) / C(n, k)` for n attempts with c successes. |
| Resolved rate | Resolved attempts ÷ all attempts. |
| Latency (mean, p95) | Wall-clock time the agent spent per attempt. Excludes environment setup and grading. |
| Tokens (in / out) | Summed over every model call in the attempt, as reported by the provider. |
| Cost | Tokens × the prices you pass in. Also reported per resolved task. |
| Steps | Model calls per attempt. |
| Tool error rate | Tool calls that returned an error ÷ all tool calls. High values point at confusing tool schemas. |
| File recall | Share of the files the maintainers changed that the agent also changed. A cheap localization signal. |
| Review warnings per patch | Pre-flight findings on the agent's diff (missing tests, missing changelog entry, unrelated files), from the same rules as `check_readiness`. Lower is better. |
| Patches that include tests | Share of attempts whose diff adds or changes a test file. |
| Lint-clean patches | Share of lint-gradable attempts whose diff passes the repo's own linter. |
| Resolved and lint-clean | Share of lint-gradable attempts that pass the maintainers' tests **and** the linter. Closer to "would be accepted" than Pass@k alone. |
| Harness errors | Attempts where setup or grading failed. These are infrastructure problems, reported separately so they are never counted as model failures silently. |

## Lint grading

Passing the maintainers' tests does not mean a patch would be accepted: most projects
also reject changes that fail their linter. After the agent finishes, the harness runs the
repo's own lint command on its diff and records the result.

An agent must not be blamed for problems that were already there, so an attempt is
lint-graded only when the task's `lint_baseline` is `clean`, meaning the linter passed on
the untouched base commit. `validate` records the baseline; for older task files it is
measured at run time. Tasks with a `dirty` or `none` baseline are left out of the two lint
metrics, and the report states how many attempts were gradable.

In the current task set, 9 of 35 tasks are lint-gradable, 21 are `dirty` and 5 have no
linter. Most `dirty` baselines are not real lint debt: the sandbox installs today's
version of the linter, which often rejects the configuration or applies newer rules to a
commit from years ago. Pinning each task's linter version would recover many of them and
is not done yet.

## Baselines

Two agents that need no model bracket every result:

- **`oracle`** writes the maintainers' fix through the server's `write_file` tool. It must
  score **100%**. Anything less means the server tools or the grader are broken.
- **`noop`** changes nothing. It must score **0%**. Anything more means a task passes
  without a fix.

Run both whenever tasks or the harness change. They cost nothing and catch most mistakes.

## Ablations: does the server help?

The claim worth testing is that an agent does better with the server's knowledge tools
than without them. `--disable-tools` withholds tools from the same model on the same tasks:

```bash
merge-eval run --agent anthropic --model <id> --k 3 --out runs/full
merge-eval run --agent anthropic --model <id> --k 3 --out runs/no-knowledge \
    --disable-tools get_playbook,find_precedents,check_readiness
merge-eval run --agent anthropic --model <id> --k 3 --out runs/no-tests \
    --disable-tools start_check,get_job
```

The disabled tools are part of the agent's name in every record, so runs cannot be mixed
up. Compare Pass@k, tool calls, tokens, review warnings and "includes tests" between the
runs. A withheld tool that the model calls anyway is refused and counted as a tool error.

The honest expectation: withholding test execution should lower Pass@k; withholding the
playbook and precedents should show up mainly in the convention measures. No ablation has
been run yet, so these are predictions.

## Determinism

What is deterministic: task contents, base commits, test selection, grading, the viability
score, the playbook, and the metrics. Each attempt starts from a fresh checkout and a
fresh virtualenv, and tests run without network access.

The sandbox also fixes `PYTHONHASHSEED`. Some test suites build their parametrized cases
from a set, so with Python's default hash randomization the generated test ids change on
every run and a recorded id points at a different case each time. The oracle baseline
caught this on a structlog task (it scored 145/150 on its own reference fix) before any
model was run. It caught a second bug the same way: test results were being parsed from
truncated output. Both are described in [design-decisions.md](design-decisions.md).

What is not: the model's output, and dependency versions. Projects with unpinned
dependencies can install newer versions over time. If a task starts failing its oracle
baseline, re-run `validate` to refresh or drop it.

## Limitations

- **Contamination.** Tasks come from public repositories, so a model may have seen the
  fix during training. Prefer recent commits when mining, and compare models on the same
  task set rather than reading a score as an absolute.
- **Python and pytest only.** The parser reads pytest's `-rA` summary.
- **Small fixes.** The miner keeps changes of at most three source files and 150 changed
  lines, which is the realistic size for a first contribution.
- **Tests are a proxy.** Passing the maintainers' tests does not prove a patch would be
  accepted. Style and scope still need human review, which is why the server stops before
  the pull request.
- **Lint grading covers a minority of tasks** (9 of 35), for the version-drift reason
  described under Lint grading.
- **Convention scoring is shallow.** Review warnings and "includes tests" come from the
  server's own pre-flight rules. They do not judge code style or commit messages.
- **Ablations withhold tools, not knowledge.** A model may already know a popular
  project's conventions from training.
