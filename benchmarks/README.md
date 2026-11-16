# Benchmark tasks

`tasks/` holds validated tasks: real issues from open-source Python projects, each with
the repository state before the fix, the maintainers' fix, and the tests that prove it.
The format and the pipeline are described in [docs/evaluation.md](../docs/evaluation.md).

## Sources

| Repository | Tasks |
|---|---|
| [python-attrs/attrs](https://github.com/python-attrs/attrs) | 5 |
| [mahmoud/boltons](https://github.com/mahmoud/boltons) | 5 |
| [tkem/cachetools](https://github.com/tkem/cachetools) | 4 |
| [python-humanize/humanize](https://github.com/python-humanize/humanize) | 4 |
| [hynek/structlog](https://github.com/hynek/structlog) | 4 |
| [jd/tenacity](https://github.com/jd/tenacity) | 4 |
| [pallets/click](https://github.com/pallets/click) | 3 |
| [marshmallow-code/marshmallow](https://github.com/marshmallow-code/marshmallow) | 3 |
| [more-itertools/more-itertools](https://github.com/more-itertools/more-itertools) | 3 |
| **Total** | **35** |

42 candidates were mined from these repositories' recent history. 7 were rejected by
validation:

- 5: the reference fix turned no failing reference test green.
- 1: the only new tests had the current time in their ids, so no later run could match them.
- 1: the reference tests hung before the fix and hit the timeout.

`baselines/` holds the oracle (35/35) and no-op (0/35) reports for this task set.

Each task records a `lint_baseline`. 9 tasks are lint-gradable (`clean`), 21 are `dirty`
and 5 have no detectable linter; see [Lint grading](../docs/evaluation.md#lint-grading).

## Provenance and licensing

Each task file embeds a short patch and an issue description taken from the repository
named in its `repo` field, at the commit named in `source_commit`. That material remains
under its original project's license and belongs to its authors; see each repository for
the terms. It is included here only so that results can be reproduced. If you maintain
one of these projects and want its tasks removed, open an issue.

## Refreshing

Dependencies of these projects are not pinned, so a task can stop installing over time.
Check with the oracle baseline, which must score 100%:

```bash
merge-eval run --agent oracle --out runs/oracle
```

Re-run `merge-eval validate benchmarks/tasks --out benchmarks/tasks` to refresh the
test lists, and delete tasks that no longer validate.
