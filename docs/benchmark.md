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

The "oracle" column replays the reference fix. It is not a model: it proves the apply, sandbox,
and regression-check machinery works with a perfect proposer (and the test suite includes negative
controls showing that a wrong fix, a fix that breaks another test, and an edit to the test file
are all *not* verified). It says nothing about how well any model writes patches.

### First real-model run (2026-10-29, after step 18)

`qwen2.5-coder:3b` through Ollama 0.40.13, CPU only (i5-8365U, 15.8 GB RAM), up to 3 attempts
per case, default settings, one run each (no repeats, so no variance estimate).

| | Dev set (7) | Held-out (4) |
|---|---|---|
| Reproduced | 7/7 | 4/4 |
| **Verified fix** | **3/7** | **0/4** |
| Mean attempts when verified | 1.33 | n/a |
| Median seconds for the fix phase | 97 | 124 |
| Wall-clock for the whole set | 10.5 min | 8.2 min |

Verified: `pagination_off_by_one`, `settings_keyerror_traceback` (both first attempt) and
`money_misdirected_traceback` (second attempt). Not verified: `lru_get_recency`,
`pricing_discount_prose`, `slugify_punctuation`, `tags_mutable_default`, and all four held-out
cases.

How to read this:
- **A 3B model fixes the simplest bugs and fails on the rest.** 3/11 overall. The held-out 0/4 is
  consistent with the dev cases being easier or the model being weak; four cases cannot separate
  the two. The retrieval change did not translate into fixes here.
- **`money_misdirected_traceback` was verified but outside the reference files.** The model
  changed a different file than the reference fix, and the whole suite still passes. Whether
  that is a legitimate alternative or a symptom-level patch is not judged by this benchmark;
  "verified" means tests pass, not that the fix is the right one.
- **I do not know why the failures failed.** This run's report did not record per-attempt
  outcomes (unparseable edit vs. search text not found vs. wrong logic vs. regression). The
  runner now records them (`attempt_outcomes`); the next run will show which it is, and that
  decides whether to work on the prompt/edit format or on the model.
- Single run, temperature as configured: results can move between runs.
- A larger model (7B+) would be slow or not fit on this machine; not measured.

### Diagnosis and repeat runs (step 20)

Same model and settings, run three times on each set (the first run above, then two more with
per-attempt outcomes recorded; the third run is after the loose-matching change below).

| | Run 1 | Run 2 | Run 3 (loose match) |
|---|---|---|---|
| Dev verified | 3/7 | 4/7 | 4/7 |
| Held-out verified | 0/4 | 0/4 | 0/4 |

- **The set of verified dev cases changes between runs.** `pagination_off_by_one` failed in run 2
  and passed in runs 1 and 3; `lru_get_recency` passed in run 2 only; `tags_mutable_default`
  failed in run 1 and passed in runs 2 and 3. Only `settings_keyerror_traceback` and `money_misdirected_traceback` passed all
  three times. Differences of one case between configurations are noise; do not read them as
  improvement or regression.
- **Where attempts fail** (runs 2 and 3 pooled, 59 attempts): 39 applied cleanly but the target
  tests still failed (wrong logic), 10 were an `invalid_edit` (search text not found), 1 had no
  edits, 1 caused a regression, and 8 verified.
  Wrong logic is the dominant failure; format problems are about one in six attempts.
- **Near-miss copies were real**: e.g. the model found the right file and line but wrote
  `{item["sku"] : item["qty"]}` for `{item["sku"]: item["qty"]}`, and in another case rewrote
  `re.sub(r"\s+", ...)` with single quotes and an invented pattern.
- **Change:** if the exact search text is not found, edits are matched ignoring whitespace layout
  and quote style (word characters are never joined or split, and the match must still be
  unique). Exact matches always win. This is tested and does not weaken verification (the
  tests still have to pass), but **its benefit is not demonstrated**: the invalid-edit rate
  was 5/29 attempts before and 5/30 after, within noise, and the verified counts did not change.
- **Conclusion so far:** the 3B model's main limit is the logic of its fixes, not the edit
  format. More runs per configuration (at least 5) are needed before comparing prompts or models.


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
