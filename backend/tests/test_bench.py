import json

import pytest

from app.config import settings
from bench.cases import load_case, load_cases
from bench.oracle import OracleProvider
from bench.runner import (
    CaseResult,
    diff_files,
    markdown_table,
    run_case,
    scratch_workspace,
    summarize,
    validate_case,
)
from tests.helpers import requires_docker


@pytest.fixture(scope="module")
def cases():
    return {case.id: case for case in load_cases()}


class ScriptedProvider:
    """Replies with a fixed JSON proposal, to act as a deliberately bad model."""

    name = "scripted"
    model = "scripted"

    def __init__(self, edits: list[dict]):
        self.reply = json.dumps({"explanation": "scripted", "edits": edits})

    def available(self):
        return True, ""

    def generate(self, prompt, *, system=None, schema=None, max_tokens=700, temperature=None):
        return self.reply


@pytest.fixture
def workspace():
    previous = settings.workspace_dir
    with scratch_workspace():
        yield
    assert settings.workspace_dir == previous


# --- Case format ---------------------------------------------------------------------------------


def test_cases_are_well_formed(cases):
    assert len(cases) >= 7
    assert {c.kind for c in cases.values()} >= {"identifier", "traceback", "prose", "misdirected"}
    for case in cases.values():
        assert case.title and case.body and case.fix_edits
        for path in case.expected_files:
            assert (case.repo_dir / path).is_file(), (case.id, path)
        for node in case.failing_tests:
            assert (case.repo_dir / node.split("::")[0]).is_file(), (case.id, node)
        # The reference fix must obey the same rules as a model's patch.
        assert not any(e.path.startswith("tests/") for e in case.fix_edits)


def test_load_cases_filters_and_rejects_unknown_ids():
    assert [c.id for c in load_cases(only=["lru_get_recency"])] == ["lru_get_recency"]
    with pytest.raises(SystemExit):
        load_cases(only=["nope"])


def test_case_files_use_unix_line_endings(cases):
    for case in cases.values():
        for path in case.repo_dir.rglob("*.py"):
            assert b"\r\n" not in path.read_bytes(), path


# --- Metrics -------------------------------------------------------------------------------------


def result(
    case_id="c", rank=None, reproduced=None, attempted=False, verified=None, attempts=None, **kw
):
    return CaseResult(
        case_id=case_id,
        kind="identifier",
        rank=rank,
        reproduced=reproduced,
        fix_attempted=attempted,
        verified=verified,
        attempts=attempts,
        **kw,
    )


def test_summarize_retrieval_metrics():
    summary = summarize(
        [result("a", rank=1), result("b", rank=3), result("c", rank=None), result("d", rank=5)]
    )
    assert summary["retrieval"] == {"hit@1": 0.25, "hit@3": 0.5, "hit@5": 0.75, "mrr": 0.383}
    assert summary["reproduction_rate"] is None  # the sandbox was never used
    assert summary["fix"]["attempted"] == 0


def test_summarize_fix_metrics_count_unreproduced_cases_as_failures():
    results = [
        result(
            "a",
            rank=1,
            reproduced=True,
            attempted=True,
            verified=True,
            attempts=2,
            fix_in_expected_files=True,
        ),
        result("b", rank=1, reproduced=True, attempted=True, verified=False, attempts=3),
        result("c", rank=1, reproduced=False),
        result(
            "d",
            rank=1,
            reproduced=True,
            attempted=True,
            verified=True,
            attempts=1,
            fix_in_expected_files=False,
        ),
    ]
    summary = summarize(results)
    assert summary["reproduction_rate"] == 0.75
    assert summary["fix"]["attempted"] == 3 and summary["fix"]["verified"] == 2
    # Out of every reproduced case, not just the ones where a fix was attempted.
    assert summary["fix"]["verified_rate_of_all_cases"] == round(2 / 4, 3)
    assert summary["fix"]["mean_attempts_when_verified"] == 1.5
    assert summary["fix"]["in_expected_files_rate"] == 0.5


def test_errors_are_counted_separately_from_misses():
    summary = summarize([result("a", rank=1), result("b", error="boom")])
    assert summary["errors"] == 1
    assert summary["retrieval"]["hit@1"] == 1.0  # the errored case does not dilute the score


def test_markdown_table_and_diff_files():
    table = markdown_table(
        [result("a", rank=2, reproduced=True, verified=False, attempts=3), result("b")]
    )
    assert "| a | identifier | 2 | yes | no | 3 |" in table
    assert "| b | identifier | miss | - | - | - |" in table
    assert diff_files("--- a/x/y.py\n+++ b/x/y.py\n@@\n-a\n+b\n") == {"x/y.py"}


# --- Retrieval baseline (needs the database, not Docker) -----------------------------------------

BASELINE_RANKS = {
    "lru_get_recency": 1,
    "pagination_off_by_one": 1,
    "pricing_discount_prose": 1,
    "slugify_punctuation": 1,
    "tags_mutable_default": 1,
}


@pytest.fixture(scope="module")
def retrieval(cases):
    return {cid: run_case(case, None, sandbox=False) for cid, case in cases.items()}


def test_retrieval_baseline_has_not_regressed(retrieval):
    assert not [r.error for r in retrieval.values() if r.error]
    for case_id, expected_rank in BASELINE_RANKS.items():
        assert retrieval[case_id].rank == expected_rank, case_id
    assert retrieval["settings_keyerror_traceback"].rank in (1, 2)
    assert summarize(list(retrieval.values()))["retrieval"]["hit@5"] >= 0.85


# Known weaknesses, found by this benchmark. They are strict expected failures so that fixing
# one makes the suite fail until the baseline above is deliberately updated.


@pytest.mark.xfail(strict=True, reason="all traceback frames score alike; the innermost should win")
def test_known_weakness_innermost_traceback_frame_should_rank_first(retrieval):
    assert retrieval["settings_keyerror_traceback"].rank == 1


@pytest.mark.xfail(
    strict=True, reason="the root cause is a callee that is never named in the issue"
)
def test_known_weakness_misdirected_root_cause_is_not_retrieved(retrieval):
    assert retrieval["money_misdirected_traceback"].rank is not None


# --- Sandbox-backed checks -----------------------------------------------------------------------


@requires_docker
def test_every_case_is_a_genuine_fixable_bug(cases, workspace):
    problems = {cid: validate_case(case) for cid, case in cases.items()}
    assert {cid: p for cid, p in problems.items() if p} == {}


@requires_docker
def test_oracle_proposer_verifies_through_the_real_pipeline(cases, workspace):
    case = cases["pagination_off_by_one"]
    outcome = run_case(case, OracleProvider(case))
    assert outcome.error is None
    assert (outcome.reproduced, outcome.verified, outcome.attempts) == (True, True, 1)
    assert outcome.fix_in_expected_files is True


@requires_docker
def test_a_wrong_fix_is_not_verified(cases, workspace):
    case = cases["pagination_off_by_one"]
    wrong = ScriptedProvider(
        [
            {
                "path": "libapp/pagination.py",
                "search": "    start = page * per_page",
                "replace": "    start = page + per_page",
            }
        ]
    )
    outcome = run_case(case, wrong)
    assert outcome.reproduced is True
    assert outcome.verified is False
    assert outcome.attempts == settings.patch_max_attempts


@requires_docker
def test_a_fix_that_breaks_other_tests_is_not_verified(cases, workspace):
    case = cases["pagination_off_by_one"]
    # Fixes the target tests but breaks page_count, which another test covers.
    regress = ScriptedProvider(
        [
            {
                "path": "libapp/pagination.py",
                "search": "    start = page * per_page",
                "replace": "    start = (page - 1) * per_page",
            },
            {
                "path": "libapp/pagination.py",
                "search": "    return (total_items + per_page - 1) // per_page",
                "replace": "    return total_items // per_page",
            },
        ]
    )
    outcome = run_case(case, regress)
    assert outcome.verified is False


@requires_docker
def test_editing_the_tests_cannot_make_a_case_pass(cases, workspace):
    case = cases["pagination_off_by_one"]
    cheat = ScriptedProvider(
        [
            {
                "path": "tests/test_pagination.py",
                "search": "== list(range(1, 11))",
                "replace": "== list(range(11, 21))",
            }
        ]
    )
    outcome = run_case(case, cheat)
    assert outcome.verified is False
    assert (case.repo_dir / "tests/test_pagination.py").read_text().count("list(range(1, 11))") == 1


def test_loading_a_single_case_directly(cases):
    case = load_case(cases["lru_get_recency"].repo_dir.parent)
    assert case.id == "lru_get_recency"
