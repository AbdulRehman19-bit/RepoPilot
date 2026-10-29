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
| Retrieval hit@1 / hit@3 / hit@5 (before step 18) | 0.71 / 0.86 / 0.86 | same |
| Retrieval MRR (before step 18) | 0.79 | same |
| Reproduction rate | 7/7 | 7/7 |
| Verified-fix rate | n/a (no model) | 7/7 |
| Median seconds: retrieval / reproduce / fix | 0.02 / 4.4 / n/a | 0.02 / 4.3 / 4.2 |

**Real-model results: none yet.** Ollama is not installed on the development machine, so no
language model has been measured. The "oracle" column replays the reference fix. It is not a
model: it proves the apply, sandbox, and regression-check machinery works with a perfect proposer
(and the test suite includes negative controls showing that a wrong fix, a fix that breaks another
test, and an edit to the test file are all *not* verified). It says nothing about how well any
model writes patches.

## What it found, and what changed (step 18)

1. **Traceback frames were weighted equally** (`settings_keyerror_traceback`, rank 2). The frame
   where the exception was raised lost to the caller's frame by 0.9 points. *Change:* when a
   traceback names two or more project files, the last one (Python prints the innermost call
   last) gets +2.0.
2. **Callees that are never named were invisible** (`money_misdirected_traceback`, miss). The fix
   is in `numbers.py`, which the issue never mentions. *Change:* for the top two matches, the
   functions inside the focused code (the symbol the issue names or the traceback line is in)
   are looked up, and files defining them get +4.0 (weaker than any direct mention). Names
   defined in more than two files are ignored.

Both used to be pinned as strict expected-failures; those are now ordinary regression tests.

### Held-out cases (`backend/bench/cases_heldout/`)

Four further bugs, written and validated *before* the ranker was changed, kept in a separate
directory, and not used to choose the two constants above (+2.0 and +4.0 were picked in advance,
not fitted). `python -m bench run --cases bench/cases_heldout`.

| | Before | After |
|---|---|---|
| Dev set (7 cases) hit@1 / hit@3 / MRR | 0.71 / 0.86 / 0.79 | 0.86 / 1.00 / 0.91 |
| Held-out (4 cases) hit@1 / hit@3 / MRR | 0.00 / 0.75 / 0.40 | 0.50 / 1.00 / 0.75 |

No dev case got worse. The dev-set gain is not evidence, since the dev cases are where the
weaknesses were found. The held-out gain is the number to look at, with the caveats that
follow.

- **Four cases is far too few** to separate a real gain from luck; one case changes hit@1 by 0.25.
- **The same person wrote them**, after deciding what to build. They test whether the features
  generalise to new layouts, not whether they help on real-world bugs.
- I predicted `scheduler_caller_heavily_named` would stay at rank 2 (the caller has more named
  symbols). It ranks 1. I did not investigate why; treat it as luck until shown otherwise.
- Both callee cases rank 2, not 1: the file the issue names still outranks the real root cause,
  by design, since a mentioned file is better evidence than a followed call.
- Following calls only works for Python, one hop deep, and only when the function is found by
  simple name. Calls through variables, decorators, dynamic dispatch or other languages are missed.

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
- Grow the held-out set with real bugs, which is the only thing that will say whether step 18
  helped outside hand-built cases.
- Run the `ollama` mode for each candidate model and record hit/fix rates and wall-clock time
  on this CPU-only machine.
