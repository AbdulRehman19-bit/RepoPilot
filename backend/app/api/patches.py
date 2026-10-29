from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.llm.factory import get_provider
from app.models import Issue, Patch, TestExecution
from app.worker.queue import enqueue_patch

router = APIRouter(tags=["patches"])

SessionDep = Annotated[Session, Depends(get_session)]
ACTIVE = ("queued", "running")


class PatchOut(BaseModel):
    id: int
    issue_id: int
    repository_id: int
    reproduction_id: int | None
    status: str
    commit_sha: str | None
    model_name: str | None
    explanation: str | None
    diff: str | None
    attempts: list[dict[str, Any]]
    error: str | None


class PatchIn(BaseModel):
    reproduction_id: int | None = None


def _out(patch: Patch) -> PatchOut:
    return PatchOut(
        id=patch.id,
        issue_id=patch.issue_id,
        repository_id=patch.repository_id,
        reproduction_id=patch.reproduction_id,
        status=patch.status,
        commit_sha=patch.commit_sha,
        model_name=patch.model_name,
        explanation=patch.explanation,
        diff=patch.diff,
        attempts=patch.attempts or [],
        error=patch.error,
    )


def _reproduced(execution: TestExecution) -> bool:
    return (
        execution.kind == "reproduction"
        and execution.status == "completed"
        and execution.exit_code not in (0, 5, None)
        and not execution.timed_out
    )


@router.post("/issues/{issue_id}/patch", status_code=202)
def start_patch(issue_id: int, session: SessionDep, body: PatchIn | None = None) -> PatchOut:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    repo = issue.repository
    if repo.index_status != "ready":
        raise HTTPException(status_code=409, detail="repository has not been indexed yet")
    if get_provider() is None:
        raise HTTPException(status_code=409, detail="the model is disabled; patches need a model")

    wanted = body.reproduction_id if body else None
    if wanted is not None:
        reproduction = session.get(TestExecution, wanted)
        if reproduction is None or reproduction.repository_id != repo.id:
            raise HTTPException(status_code=404, detail="reproduction not found for this issue")
    else:
        reproduction = session.scalar(
            select(TestExecution)
            .where(
                TestExecution.issue_id == issue.id,
                TestExecution.kind == "reproduction",
                TestExecution.status == "completed",
            )
            .order_by(TestExecution.id.desc())
        )
    if reproduction is None or not _reproduced(reproduction):
        raise HTTPException(
            status_code=409,
            detail="the issue has not been reproduced yet; run POST /issues/{id}/reproduce first",
        )

    busy = session.scalar(
        select(Patch.id).where(Patch.repository_id == repo.id, Patch.status.in_(ACTIVE))
    )
    if busy is not None:
        raise HTTPException(status_code=409, detail="a patch is already being generated")

    patch = Patch(
        issue_id=issue.id, repository_id=repo.id, reproduction_id=reproduction.id, status="queued"
    )
    session.add(patch)
    session.commit()
    try:
        enqueue_patch(patch.id)
    except Exception as exc:  # noqa: BLE001 - surface any queue failure on the record
        patch.status, patch.error = "error", f"could not enqueue patch job: {exc}"
        session.commit()
    return _out(patch)


@router.get("/patches/{patch_id}")
def get_patch(patch_id: int, session: SessionDep) -> PatchOut:
    patch = session.get(Patch, patch_id)
    if patch is None:
        raise HTTPException(status_code=404, detail="patch not found")
    return _out(patch)


@router.get("/issues/{issue_id}/patches")
def list_patches(issue_id: int, session: SessionDep) -> list[PatchOut]:
    rows = session.scalars(
        select(Patch).where(Patch.issue_id == issue_id).order_by(Patch.id.desc()).limit(20)
    )
    return [_out(row) for row in rows]
