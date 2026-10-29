"""Run benchmark cases through the real RepoPilot pipeline and score them."""

import statistics
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.indexing.indexer import index_repository
from app.llm.base import LLMProvider
from app.models import Issue, Repository, User
from app.patching.apply import apply_edits, make_candidate
from app.patching.loop import generate_verified_patch
from app.retrieval.service import investigate
from app.sandbox.deps import prepare_dependencies
from app.sandbox.pytest_runner import run_pytest
from bench.cases import Case

RANK_LIMIT = 10
ProviderFactory = Callable[[Case], LLMProvider | None]


@dataclass
class CaseResult:
    case_id: str
    kind: str
    retrieved: list[str] = field(default_factory=list)
    rank: int | None = None  # 1-based rank of the best-ranked expected file
    reproduced: bool | None = None  # None when the sandbox was skipped
    fix_attempted: bool = False
    verified: bool | None = None
    attempts: int | None = None
    fix_in_expected_files: bool | None = None
    attempt_outcomes: list[str] = field(default_factory=list)  # e.g. invalid_edit, target_failed
    seconds: dict[str, float] = field(default_factory=dict)
    error: str | None = None


@contextmanager
def scratch_session() -> Iterator[Session]:
    """A database session whose writes are always rolled back, so benchmarks leave no trace."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@contextmanager
def scratch_workspace() -> Iterator[Path]:
    """Point the sandbox scratch space at a temporary directory for the duration of a run."""
    previous = settings.workspace_dir
    with tempfile.TemporaryDirectory(prefix="repopilot-bench-") as tmp:
        settings.workspace_dir = Path(tmp)
        try:
            yield Path(tmp)
        finally:
            settings.workspace_dir = previous


def diff_files(diff: str) -> set[str]:
    return {line[6:] for line in diff.splitlines() if line.startswith("+++ b/")}


def run_case(case: Case, provider: LLMProvider | None, *, sandbox: bool = True) -> CaseResult:
    result = CaseResult(case_id=case.id, kind=case.kind)
    try:
        _run_case(case, provider, sandbox, result)
    except Exception as exc:  # noqa: BLE001 - one broken case must not stop the whole run
        result.error = f"{type(exc).__name__}: {exc}"[:500]
    return result


def _run_case(case: Case, provider: LLMProvider | None, sandbox: bool, result: CaseResult) -> None:
    with scratch_session() as session:
        owner = User(github_id=-1, login="bench")
        repo = Repository(owner=owner, github_id=-2, full_name=f"bench/{case.id}")
        issue = Issue(repository=repo, number=1, title=case.title, body=case.body)
        session.add_all([owner, repo, issue])
        session.flush()

        started = time.monotonic()
        index_repository(session, repo, case.repo_dir, "bench")
        result.seconds["index"] = round(time.monotonic() - started, 3)

        started = time.monotonic()
        ranked = investigate(session, issue, limit=RANK_LIMIT)
        result.seconds["retrieval"] = round(time.monotonic() - started, 3)
        result.retrieved = [m.path for m in ranked.matches]
        ranks = [i for i, p in enumerate(result.retrieved, start=1) if p in case.expected_files]
        result.rank = min(ranks) if ranks else None

        if not sandbox:
            return

        # The same inputs the real patch job would use (default limit and budget).
        context = investigate(session, issue)
        test_sources = {
            path: (case.repo_dir / path).read_text(encoding="utf-8")
            for path in dict.fromkeys(t.split("::")[0] for t in case.failing_tests)
        }

    started = time.monotonic()
    deps = prepare_dependencies(case.repo_dir)
    baseline = run_pytest(case.repo_dir, deps, [])
    target = run_pytest(case.repo_dir, deps, list(case.failing_tests))
    result.seconds["reproduce"] = round(time.monotonic() - started, 3)
    failing = {f.node_id for f in target.failures}
    result.reproduced = (
        not target.all_passed and not target.no_tests_ran and set(case.failing_tests) <= failing
    )

    if provider is None or not result.reproduced:
        return

    result.fix_attempted = True
    started = time.monotonic()
    outcome = generate_verified_patch(
        provider=provider,
        checkout=case.repo_dir,
        deps=deps,
        title=case.title,
        body=case.body,
        matches=context.matches,
        blocks=context.blocks,
        selectors=list(case.failing_tests),
        failure_output=target.output,
        test_sources=test_sources,
        baseline_failures={f.node_id for f in baseline.failures},
    )
    result.seconds["fix"] = round(time.monotonic() - started, 3)
    result.verified = outcome.verified
    result.attempts = len(outcome.attempts)
    result.attempt_outcomes = [a.outcome for a in outcome.attempts]
    if outcome.verified:
        result.fix_in_expected_files = diff_files(outcome.diff) <= set(case.expected_files)


def validate_case(case: Case) -> list[str]:
    """Check that a case is a genuine, fixable bug. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    for path in case.expected_files:
        if not (case.repo_dir / path).is_file():
            problems.append(f"expected file {path} is missing from the repo")
    if not case.title.strip():
        problems.append("the issue has no title")
    if problems:
        return problems

    deps = prepare_dependencies(case.repo_dir)
    baseline = run_pytest(case.repo_dir, deps, [])
    baseline_failing = {f.node_id for f in baseline.failures}
    if baseline_failing != set(case.failing_tests):
        problems.append(
            f"before the fix the failing tests are {sorted(baseline_failing)}, "
            f"expected exactly {sorted(case.failing_tests)}"
        )

    protected = {t.split("::")[0] for t in case.failing_tests}
    with scratch_workspace() as scratch:
        candidate = make_candidate(case.repo_dir, scratch / "candidate")
        applied = apply_edits(candidate, list(case.fix_edits), protected)
        if not set(applied.files) <= set(case.expected_files):
            problems.append(f"the reference fix edits {applied.files}, not {case.expected_files}")
        fixed = run_pytest(candidate, deps, [])
        if not fixed.all_passed:
            problems.append(
                f"with the reference fix the suite still fails: {[f.node_id for f in fixed.failures]}"
            )
    return problems


def summarize(results: list[CaseResult]) -> dict:
    n = len(results)
    ranked = [r for r in results if r.error is None]

    def share(count: int, total: int) -> float | None:
        return round(count / total, 3) if total else None

    def hit(k: int) -> float | None:
        return share(sum(1 for r in ranked if r.rank is not None and r.rank <= k), len(ranked))

    reciprocal = [1 / r.rank if r.rank else 0.0 for r in ranked]
    sandboxed = [r for r in ranked if r.reproduced is not None]
    attempted = [r for r in ranked if r.fix_attempted]
    verified = [r for r in attempted if r.verified]

    def median(phase: str) -> float | None:
        values = [r.seconds[phase] for r in ranked if phase in r.seconds]
        return round(statistics.median(values), 2) if values else None

    return {
        "cases": n,
        "errors": n - len(ranked),
        "retrieval": {
            "hit@1": hit(1),
            "hit@3": hit(3),
            "hit@5": hit(5),
            "mrr": round(sum(reciprocal) / len(ranked), 3) if ranked else None,
        },
        "reproduction_rate": share(sum(1 for r in sandboxed if r.reproduced), len(sandboxed)),
        "fix": {
            "attempted": len(attempted),
            "verified": len(verified),
            "verified_rate_of_all_cases": share(len(verified), len(sandboxed))
            if attempted
            else None,
            "mean_attempts_when_verified": (
                round(statistics.mean(r.attempts for r in verified), 2) if verified else None
            ),
            "in_expected_files_rate": share(
                sum(1 for r in verified if r.fix_in_expected_files), len(verified)
            ),
        },
        "median_seconds": {p: median(p) for p in ("index", "retrieval", "reproduce", "fix")},
    }


def markdown_table(results: list[CaseResult]) -> str:
    rows = [
        "| case | kind | rank | reproduced | verified | attempts |",
        "|---|---|---|---|---|---|",
    ]

    def cell(value) -> str:
        return (
            "-"
            if value is None
            else ("yes" if value is True else "no" if value is False else str(value))
        )

    for r in results:
        rank = r.rank if r.rank else ("miss" if r.error is None else "error")
        rows.append(
            f"| {r.case_id} | {r.kind} | {rank} | {cell(r.reproduced)} | {cell(r.verified)} | {cell(r.attempts)} |"
        )
    return "\n".join(rows)


def as_dicts(results: list[CaseResult]) -> list[dict]:
    return [asdict(r) for r in results]
