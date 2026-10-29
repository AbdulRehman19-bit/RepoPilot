from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.llm.factory import get_provider
from app.models import IndexedFile, Issue, Patch, Repository, TestExecution
from app.patching.loop import generate_verified_patch
from app.retrieval.service import investigate
from app.sandbox.deps import prepare_dependencies
from app.sandbox.pytest_runner import run_pytest
from app.security.redact import redact
from app.worker.execution_jobs import MAX_STORED_OUTPUT_CHARS

TEST_SOURCE_LINES = 80
MAX_TEST_FILES = 2


def _record_baseline(session, repo: Repository, checkout, deps) -> set[str]:
    """Failing tests in the unpatched repository, reusing a recorded baseline for this commit."""
    existing = session.scalar(
        select(TestExecution)
        .where(
            TestExecution.repository_id == repo.id,
            TestExecution.kind == "baseline",
            TestExecution.status == "completed",
            TestExecution.commit_sha == repo.indexed_commit,
        )
        .order_by(TestExecution.id.desc())
    )
    if existing is not None:
        return {f["node_id"] for f in existing.failures or []}

    outcome = run_pytest(checkout, deps, [])
    session.add(
        TestExecution(
            repository_id=repo.id,
            kind="baseline",
            status="completed",
            selectors=[],
            commit_sha=repo.indexed_commit,
            exit_code=outcome.exit_code,
            passed=outcome.passed,
            failed=outcome.failed,
            errors=outcome.errors,
            skipped=outcome.skipped,
            timed_out=outcome.timed_out,
            duration_seconds=outcome.duration_seconds,
            failures=[
                {"kind": f.kind, "node_id": f.node_id, "message": f.message}
                for f in outcome.failures
            ],
            output=outcome.output[:MAX_STORED_OUTPUT_CHARS],
        )
    )
    session.commit()
    return {f.node_id for f in outcome.failures}


def _test_sources(session, repo_id: int, selectors: list[str]) -> dict[str, str]:
    paths = list(dict.fromkeys(s.split("::")[0] for s in selectors))[:MAX_TEST_FILES]
    sources: dict[str, str] = {}
    for path in paths:
        content = session.scalar(
            select(IndexedFile.content).where(
                IndexedFile.repository_id == repo_id, IndexedFile.path == path
            )
        )
        if content:
            sources[path] = "\n".join(content.splitlines()[:TEST_SOURCE_LINES])
    return sources


def run_patch_job(patch_id: int) -> None:
    """Generate a patch for an issue and verify it in the sandbox."""
    with SessionLocal() as session:
        patch = session.get(Patch, patch_id)
        if patch is None:
            return
        patch.status, patch.error = "running", None
        session.commit()

        try:
            issue = session.get(Issue, patch.issue_id)
            repo = session.get(Repository, patch.repository_id)
            reproduction = session.get(TestExecution, patch.reproduction_id)
            provider = get_provider()
            if provider is None:
                raise RuntimeError("the model is disabled (REPOPILOT_LLM_PROVIDER=none)")
            ok, reason = provider.available()
            if not ok:
                raise RuntimeError(reason)
            checkout = settings.workspace_dir / str(repo.id)
            if not checkout.is_dir():
                raise RuntimeError("repository checkout is missing; re-index the repository")

            found = investigate(session, issue)
            deps = prepare_dependencies(checkout)
            baseline_failures = _record_baseline(session, repo, checkout, deps)

            outcome = generate_verified_patch(
                provider=provider,
                checkout=checkout,
                deps=deps,
                title=issue.title,
                body=issue.body,
                matches=found.matches,
                blocks=found.blocks,
                selectors=reproduction.selectors,
                failure_output=reproduction.output or "",
                test_sources=_test_sources(session, repo.id, reproduction.selectors),
                baseline_failures=baseline_failures,
            )
            patch.commit_sha = repo.indexed_commit
            patch.model_name = provider.model
            patch.attempts = outcome.as_dicts()
            patch.status = "verified" if outcome.verified else "failed"
            if outcome.verified:
                patch.diff, patch.edits, patch.explanation = (
                    outcome.diff,
                    outcome.edits,
                    outcome.explanation,
                )
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded on the patch
            session.rollback()
            patch = session.get(Patch, patch_id)
            patch.status, patch.error = "error", redact(str(exc))[:2000]
        session.commit()
