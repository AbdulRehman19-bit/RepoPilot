from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.ratelimit import per_caller
from app.api.security import PrincipalDep
from app.db import get_session
from app.models import Issue, Repository, TestExecution
from app.sandbox.pytest_runner import InvalidSelector, extract_node_ids, validate_selectors
from app.worker.queue import enqueue_execution

router = APIRouter(tags=["executions"])

SessionDep = Annotated[Session, Depends(get_session)]
ACTIVE = ("queued", "running")


class FailureOut(BaseModel):
    kind: str
    node_id: str
    message: str


class ExecutionOut(BaseModel):
    id: int
    repository_id: int
    issue_id: int | None
    kind: str
    status: str
    selectors: list[str]
    commit_sha: str | None
    exit_code: int | None
    passed: int
    failed: int
    errors: int
    skipped: int
    timed_out: bool
    duration_seconds: float | None
    failures: list[FailureOut]
    warnings: list[str]
    error: str | None
    # True when the targeted tests failed, i.e. the reported problem was reproduced.
    reproduced: bool | None


class ExecutionDetailOut(ExecutionOut):
    output: str | None


class ReproduceIn(BaseModel):
    selectors: list[str] | None = None


def _reproduced(execution: TestExecution) -> bool | None:
    if execution.kind != "reproduction" or execution.status != "completed":
        return None
    return execution.exit_code not in (0, 5) and not execution.timed_out


def _out(execution: TestExecution) -> ExecutionOut:
    return ExecutionOut(
        id=execution.id,
        repository_id=execution.repository_id,
        issue_id=execution.issue_id,
        kind=execution.kind,
        status=execution.status,
        selectors=execution.selectors or [],
        commit_sha=execution.commit_sha,
        exit_code=execution.exit_code,
        passed=execution.passed,
        failed=execution.failed,
        errors=execution.errors,
        skipped=execution.skipped,
        timed_out=execution.timed_out,
        duration_seconds=execution.duration_seconds,
        failures=[FailureOut(**f) for f in execution.failures or []],
        warnings=execution.warnings or [],
        error=execution.error,
        reproduced=_reproduced(execution),
    )


def _start(
    session: Session, repo: Repository, kind: str, selectors: list[str], issue_id: int | None
) -> ExecutionOut:
    if repo.index_status != "ready":
        raise HTTPException(status_code=409, detail="repository has not been indexed yet")
    busy = session.scalar(
        select(TestExecution.id).where(
            TestExecution.repository_id == repo.id, TestExecution.status.in_(ACTIVE)
        )
    )
    if busy is not None:
        raise HTTPException(status_code=409, detail="a test run is already in progress")

    execution = TestExecution(
        repository_id=repo.id, issue_id=issue_id, kind=kind, selectors=selectors, status="queued"
    )
    session.add(execution)
    session.commit()
    try:
        enqueue_execution(execution.id)
    except Exception as exc:  # noqa: BLE001 - surface any queue failure on the record
        execution.status = "error"
        execution.error = f"could not enqueue test run: {exc}"
        session.commit()
    return _out(execution)


@router.post(
    "/repositories/{repo_id}/baseline",
    status_code=202,
    dependencies=[per_caller("jobs", "rate_limit_jobs_per_hour")],
)
def run_baseline(repo_id: int, session: SessionDep, principal: PrincipalDep) -> ExecutionOut:
    repo = session.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    principal.require(repo, write=True)
    return _start(session, repo, "baseline", [], None)


@router.post(
    "/issues/{issue_id}/reproduce",
    status_code=202,
    dependencies=[per_caller("jobs", "rate_limit_jobs_per_hour")],
)
def reproduce(
    issue_id: int, session: SessionDep, principal: PrincipalDep, body: ReproduceIn | None = None
) -> ExecutionOut:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    principal.require(issue.repository, write=True)

    selectors = (body.selectors if body and body.selectors else None) or extract_node_ids(
        f"{issue.title}\n{issue.body}"
    )
    if not selectors:
        raise HTTPException(
            status_code=422,
            detail="no failing test is named in the issue; provide test selectors to reproduce it",
        )
    try:
        selectors = validate_selectors(selectors)
    except InvalidSelector as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _start(session, issue.repository, "reproduction", selectors, issue.id)


@router.get("/executions/{execution_id}")
def get_execution(
    execution_id: int, session: SessionDep, principal: PrincipalDep
) -> ExecutionDetailOut:
    execution = session.get(TestExecution, execution_id)
    if execution is None:
        raise HTTPException(status_code=404, detail="test run not found")
    principal.require(session.get(Repository, execution.repository_id))
    return ExecutionDetailOut(**_out(execution).model_dump(), output=execution.output)


@router.get("/repositories/{repo_id}/executions")
def list_executions(
    repo_id: int, session: SessionDep, principal: PrincipalDep
) -> list[ExecutionOut]:
    repo = session.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    principal.require(repo)
    rows = session.scalars(
        select(TestExecution)
        .where(TestExecution.repository_id == repo_id)
        .order_by(TestExecution.id.desc())
        .limit(50)
    )
    return [_out(row) for row in rows]
