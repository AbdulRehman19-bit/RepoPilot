# Benchmark

RepoPilot's claim is *reproducibility and verification*, so it needs a way to measure whether it
delivers. This is the first version of that measurement. It is deliberately small and its limits
are stated below; treat the numbers as a baseline to beat, not as evidence of real-world quality.

## What is measured

| Metric | Meaning |
|---|---|
| **hit@k, MRR** | Is a file the reference fix edits among the top *k* ranked files for the issue? |
| **Reproduction rate** | Does the sandbox reproduce the bug by running the named failing tests (they fail, for the right reason)? |
| **Verified-fix rate** | Of all cases, how many end with a patch that makes the failing tests pass **and** breaks no test that passed before? |
| **In-expected-files rate** | Does a verified patch edit only the files the reference fix edits? |
| **Median seconds** | Per phase: index, retrieval, reproduce, fix. |

## Cases

`backend/bench/cases/<id>/` holds a small buggy Python project, the issue text, the failing tests,
and a reference fix. `python -m bench validate` proves each case is a real bug: before the fix
exactly the listed tests fail, and with the reference fix the whole suite passes.

| Case | Issue style | What it tests |
|---|---|---|
| `pagination_off_by_one` | names `paginate` in backticks | identifier match |
| `slugify_punctuation` | names `slugify` with an example | identifier match |
| `tags_mutable_default` | names `add_tag`, describes the symptom | identifier match |
| `lru_get_recency` | names `LRUCache` | identifier match, class method |
| `settings_keyerror_traceback` | full traceback | traceback frames |
| `pricing_discount_prose` | plain prose, no code names | full-text search only |
| `money_misdirected_traceback` | traceback stops one call too early | root cause in an unnamed callee |

Each project also contains unrelated modules so that retrieval has something to get wrong.

## Running it

```bash
cd backend
uv run python -m bench validate                  # needs Docker + the sandbox image
uv run python -m bench run --provider none       # retrieval + reproduction, no model
uv run python -m bench run --provider oracle     # + the fix pipeline with a perfect proposer
uv run python -m bench run --provider ollama     # + a real local model (needs Ollama)
uv run python -m bench run --no-sandbox          # retrieval only, no Docker
```

Reports are written to `backend/bench/results/` (git-ignored).

## Results (2026-10-29)

Machine: Windows 11, Intel i5-8365U, Docker Desktop. 7 cases.

| | Model off | Oracle proposer |
|---|---|---|
| Retrieval hit@1 / hit@3 / hit@5 | 0.71 / 0.86 / 0.86 | same |
| Retrieval MRR | 0.79 | same |
| Reproduction rate | 7/7 | 7/7 |
| Verified-fix rate | n/a (no model) | 7/7 |
| Median seconds: retrieval / reproduce / fix | 0.02 / 4.4 / n/a | 0.02 / 4.3 / 4.2 |

**Real-model results: none yet.** Ollama is not installed on the development machine, so no
language model has been measured. The "oracle" column replays the reference fix. It is not a
model: it proves the apply, sandbox, and regression-check machinery works with a perfect proposer
(and the test suite includes negative controls showing that a wrong fix, a fix that breaks another
test, and an edit to the test file are all *not* verified). It says nothing about how well any
model writes patches.

## What it found

1. **Traceback frames are weighted equally** (`settings_keyerror_traceback`, rank 2). The frame
   where the exception was raised (`config.py`) lost to the frame where the call was made
   (`cli.py`) by 0.9 points, decided only by the full-text bonus. The innermost frame should
   count for more.
2. **Callees that are never named are invisible** (`money_misdirected_traceback`, miss). The
   fix belongs in `numbers.py`, which the issue never mentions; it is only reachable by following
   imports from `money.py`. A model working from the retrieved context could not fix this case.
   (The oracle "fixes" it only because it ignores context.)

Both are pinned as strict expected-failures in `backend/tests/test_bench.py`, so the suite will
fail, prompting a baseline update, when either is fixed.

## Limits of this benchmark

- **Seven cases written by the author of the system** is a smoke test, not a benchmark. They are
  small, self-contained, standard-library-only, and each has a single-line fix. Real bugs are
  larger and messier.
- **The retrieval weights were not tuned on these cases**, on purpose. Tuning on seven visible
  cases would only measure how well the weights fit those cases. The two weaknesses above should
  be fixed and evaluated against a larger, held-out set of real bugs.
- Cases have no third-party dependencies, so dependency preparation is not exercised.
- Timings are for one machine and exclude model latency.

## Next

- Add 30-50 real bugs mined from small open-source Python projects (a failing test at a known
  commit and the commit that fixed it), including some with dependencies. Keep part of them
  unseen while developing.
- Fix the two weaknesses above (innermost-frame weighting; following imports from top-ranked
  files) and measure against the new cases.
- Run the `ollama` mode for each candidate model and record hit/fix rates and wall-clock time
  on this CPU-only machine.
