# Design decisions

Each entry records a choice, the alternative that was rejected, and what the choice costs.
If you are asked about this project in an interview, these are the questions that come up.

## 1. The server and the benchmark are separate programs

**Decision.** `merge-mcp` exposes tools. `merge-eval` is an MCP client with its own
agent loop that drives those tools.

**Why.** An MCP server does not contain an agent; the client's model decides which tools
to call. In an IDE that model is outside your control, so you cannot measure it. A
headless client makes the measurement possible and tests the server through the same
protocol a real client uses.

**Cost.** Two entry points and an agent loop to maintain. The loop is about 60 lines
because it has no framework.

## 2. Handles instead of sessions

**Decision.** Cross-call state is addressed by `workspace_id` and `job_id`, passed as
ordinary arguments. State lives on disk.

**Alternative.** Keep the "current workspace" in per-connection session state.

**Why.** The MCP specification moved to a stateless model in its 2026-07-28 revision and
recommends explicit handles. Handles also survive a server restart, let several clients
share a workspace, and make evaluation runs reproducible.

**Cost.** Every tool takes one more argument, and the model must carry the id. In the
benchmark the harness injects it so the model cannot get it wrong.

## 3. Explicit job tools instead of MCP Tasks

**Decision.** `start_workspace` and `start_check` return a `JobHandle`; `get_job` polls
with optional long-polling.

**Alternative.** Mark the tools as protocol-level tasks (FastMCP `task=True`).

**Why.** Every client can call two normal tools. Tasks support varies by client, and the
Tasks design itself changed between spec revisions. The portable option was chosen for
the first release.

**Cost.** Polling uses extra model turns. Long-polling up to 60 seconds keeps that to one
or two calls. Moving to Tasks later is a change in `server.py` only, because `JobRegistry`
is independent of MCP.

## 4. Docker at the OS boundary, not an in-language sandbox

**Decision.** Repository code runs in a container with no network and no credentials.

**Alternative.** RestrictedPython or a similar in-process restriction.

**Why.** A test suite is a whole program: it imports plugins, spawns subprocesses and
loads C extensions. In-language sandboxes cannot run that, and they share a process with
the server, so one escape exposes the token. An OS-level boundary does not care what the
code inside does.

**Cost.** Docker must be installed. Container start adds about a second per check.
`MERGE_MCP_SANDBOX=local` exists for machines that are already disposable, and it is never
selected silently.

## 5. The human gate is structural

**Decision.** There is no commit, push, fork or pull request tool, and the token is
read-only.

**Alternative.** Provide a `create_pull_request` tool and instruct the model to ask first.

**Why.** Instructions can be ignored or overridden by text in an issue. A capability that
does not exist cannot be misused. Maintainers are also increasingly strict about
unreviewed AI pull requests, so "a human submits every change" is a feature for the
people on the receiving end.

**Cost.** The user runs four commands by hand. The server prints them.

## 6. No arbitrary shell tool

**Decision.** The agent can run three things: the repo's setup, tests and linter.

**Alternative.** A general `run_command` tool, which most coding agents have.

**Why.** Issue text is untrusted input that can contain instructions. With no shell, the
worst a hijacked agent can do is edit files in a throwaway checkout. Fewer degrees of
freedom also make benchmark runs easier to compare.

**Cost.** The agent cannot run ad-hoc scripts to explore behavior. It can still write a
test and run it, which is the workflow maintainers want anyway.

## 7. Rules, not an LLM, for scoring and conventions

**Decision.** The viability score and the playbook are computed from API data, repo files
and `git log` by explicit rules.

**Alternative.** Ask a model to read `CONTRIBUTING.md` and summarize the conventions.

**Why.** Rules are reproducible, free, fast and testable. The same repo state always
yields the same playbook, so it can be cached and it does not add noise to the benchmark.
Every point of the viability score comes with a stated reason.

**Cost.** Rules miss conventions written only in prose. The playbook lists the section
headings of the contributing guide and its path so the client's model can read the
original when needed.

## 8. Fact gathering is separate from scoring

**Decision.** `collect_signals` does I/O and returns a `ViabilitySignals` model. `score`
is a pure function of that model.

**Why.** The scoring rules can be tested exhaustively with no network, and changing a
weight never touches API code.

## 9. Outcome-based grading only

**Decision.** An attempt is resolved if and only if the maintainers' tests pass.

**Alternatives.** Diff similarity to the reference fix, or an LLM judge.

**Why.** A correct fix can look nothing like the reference, and an LLM judge adds its own
errors and cost. Tests are the criterion the maintainers themselves used.

**Cost.** Tests can be incomplete, so a wrong patch can occasionally pass. File recall is
reported next to Pass@k as a secondary signal.

## 10. Tasks must be executed before they count

**Decision.** `mine` produces candidates. Only `validate`, which runs the tests before and
after the reference fix (twice), can mark a task usable.

**Why.** Mined history is noisy: patches that do not apply to the parent, environments
that no longer install, tests that pass without the fix, flaky tests. A benchmark with
broken tasks produces confident wrong numbers.

**Cost.** Validation is slow (an install and three test runs per candidate). In the first
mining pass it rejected 7 of 42 candidates, which is the step working.

## 11. Oracle and no-op baselines

**Decision.** Ship two model-free agents that must score 100% and 0%.

**Why.** They test the harness itself. If the oracle falls short, the tools or the grader
are wrong; if the no-op scores, a task is wrong. They run for free in CI.

## 12. The agent's test edits are discarded before grading

**Decision.** Before grading, reference test files are restored and the maintainers'
`test_patch` is applied on top.

**Why.** Otherwise the cheapest way to pass is to weaken the test. There is a regression
test for exactly this (`test_agent_cannot_pass_by_weakening_tests`).

## 13. A provider-neutral transcript

**Decision.** The agent loop stores `Turn` objects. Each provider converts them to its own
wire format on every call.

**Why.** Adding a provider is one method. The OpenAI-compatible provider also covers local
models, so the benchmark can be run at zero API cost.

**Cost.** The transcript is re-serialized on every call. That is negligible next to the
model call itself.

## 14. `workspace_id` is hidden from the evaluated model

**Decision.** The harness strips `workspace_id` from tool schemas and injects it.

**Why.** It removes a class of pointless tool errors, saves tokens, and prevents an agent
from touching another attempt's workspace when attempts run concurrently.

## 15. The editable install runs last

**Decision.** The detected install command installs dev dependencies first and the
project itself (`pip install -e .`) last.

**Why.** Found while testing against a real repo: a dev dependency can depend on the
project being tested (pytest depends on iniconfig), so installing it afterwards replaces
the checkout with the PyPI release and the tests silently run against the wrong code.

## 16. A fixed hash seed in the sandbox

**Decision.** Every sandboxed command runs with `PYTHONHASHSEED=0`.

**Why.** Found by the oracle baseline: it scored 145/150 on a structlog task using the
maintainers' own fix. That suite parametrizes tests from a set, so Python's hash
randomization gave the cases different ids on each run and the recorded
`fail_to_pass` ids pointed at different cases. Fixing the seed makes ids stable.

**Cost.** Tests run under one hash ordering instead of a random one, so an
order-dependent bug in a target project could be hidden. That is acceptable for a
benchmark, where repeatability matters more.

## 17. Parse results from full output, trim only what the model sees

**Decision.** The runner keeps the complete output of a command (up to a 5 MB memory
guard). Per-test outcomes are parsed from all of it. Only the `output_tail` field returned
to the model is trimmed to 6,000 characters.

**Why.** Also found by the oracle baseline. An earlier version trimmed output to 20,000
characters *before* parsing, so a verbose suite lost some of its result lines and a correct
fix scored 30/38. Because the amount of output varied slightly between runs, it looked
like flaky tests. The two concerns are different: the model needs a short tail, the grader
needs everything.

## 18. Policy as data: a rules file instead of constants

**Decision.** Scoring weights, language ecosystems, CI step patterns, file categories and
policy patterns live in `defaults.toml`. A user file is deep-merged over it and the result
is validated with Pydantic at start-up.

**Alternative.** Constants and `if language == ...` branches in the code.

**Why.** These values are opinions, and other people will disagree with them or need a
language that is not covered. Changing an opinion should not require a code change, a
review and a release. Validation turns a typo into a start-up error that names the key,
instead of a silent change in behaviour.

**Cost.** One more concept to learn, and regular expressions in a config file are easy to
get wrong. The loader is 30 lines; the models that validate it are the documentation.

## 19. The repo's CI workflow is the specification for how to build it

**Decision.** Install, test and lint commands are read from `.github/workflows/` before
any language default is used.

**Alternative.** Per-language detection (the first version did this for Python only).

**Why.** Per-language detection means writing and maintaining a detector for every
ecosystem, and still guessing. CI is the one place a project must state its commands
correctly, in whatever language it uses.

**Cost.** It is a heuristic over a format designed for another runner. Tried on ten
well-known repositories it picked a sensible job for most; the rest use matrices of
custom scripts, build artefacts passed between jobs, or wrappers. Three guards keep
failures cheap: unreproducible steps are dropped and reported, CI-derived setup falls
back once to language defaults, and every command can be overridden.

## 20. Python keeps its detector, and CI wrappers are not adopted

**Decision.** For Python, a CI test command is used only when it calls pytest directly.
`tox`, `nox` and `uv run` commands are ignored in favour of the detector's direct call.

**Why.** Found on real repos: attrs and click run tests through tox with wheels built by
an earlier job, which cannot be reproduced in one container. A direct pytest call works
and also yields per-test results, which the benchmark needs. The rule is one key in the
rules file (`ci_test_must_match`), not special-case code.

## 21. Success is the exit code; per-test parsing is optional

**Decision.** A check passes when the project's command exits zero. Parsing individual
test results is a named, optional hook per ecosystem.

**Why.** Exit status is the one contract every test runner honours, so the server works
with runners it has never heard of. Only the benchmark's fail-to-pass grading needs
per-test results, and it pins pytest for that reason.

## 22. Convention rules are learned from history, not written by hand

**Decision.** "A source change should come with a test and a changelog entry" is not
hard-coded. `precedents.learn` measures how often that was true in the repo's recent
commits and reports only patterns above a support and confidence threshold.

**Why.** Projects differ: some require a changelog fragment for every change, some have no
changelog at all. A fixed checklist would nag in the second kind and miss the first.
Learned rules come with their evidence ("93% of 30 commits"), which makes a warning
credible to the person reading it.

**Cost.** Small or young repos produce no rules. Squash-merge repos give clean signals;
repos that commit tests separately from code give weak ones.

## 23. Whatever setup creates is excluded from the diff

**Decision.** Untracked files that appear during `setup` are added to the workspace's git
exclude list.

**Why.** Found on the first Node run: `npm install` wrote a `package-lock.json` and it
showed up as part of the contribution. Listing every tool's by-products would never be
complete; comparing before and after works for all of them.

## 24. The local runner inherits by pattern and refuses credentials by name

**Decision.** In `local` mode a child process inherits only locale, proxy and certificate
variables, matched by pattern, and never a variable whose name contains `TOKEN`, `KEY`,
`SECRET` and similar.

**Why.** A fixed allowlist broke `npm install` behind a TLS-inspecting proxy, because Node
reads its CA bundle from a variable the list did not have. Patterns cover each package
manager's spelling; the name check keeps that from widening into a leak.

## 25. Lint is graded only where the base commit was clean

**Decision.** The benchmark runs the repo's own linter on the agent's diff, but counts the
result only for tasks whose untouched base commit passes that linter.

**Alternative.** Count new lint errors by comparing linter output before and after.

**Why.** Passing tests is not acceptance; a maintainer also rejects a patch that fails
lint. But many repos fail their own linter before any change, and comparing error counts
means parsing every linter's output format. Restricting the measure to clean baselines
needs no parsing, works for any linter, and never blames the agent for existing problems.

**Cost.** Coverage. Only 9 of the 35 current tasks qualify, mostly because today's linter
version disagrees with old commits' configuration. The report states the number of
gradable attempts next to the rate so the small sample is visible.

## What I would do next

- Pin each task's linter version so more tasks are lint-gradable.

- Cache built environments per `(repo, base_commit)` to cut benchmark setup time.
- Support MCP Tasks alongside the job tools once client support is uniform.
- Benchmark tasks in a second language: needs a per-test result parser for that runner.
- Read CI systems other than GitHub Actions.
- Run the ablation and publish the numbers.
