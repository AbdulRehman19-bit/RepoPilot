from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.security import PrincipalDep
from app.db import get_session
from app.models import IndexedFile, Repository
from app.worker.queue import enqueue_index

router = APIRouter(prefix="/repositories", tags=["repositories"])

SessionDep = Annotated[Session, Depends(get_session)]


class RepositoryOut(BaseModel):
    id: int
    full_name: str
    default_branch: str
    index_status: str
    index_error: str | None
    indexed_commit: str | None
    file_count: int


def _to_out(repo: Repository, file_count: int) -> RepositoryOut:
    return RepositoryOut(
        id=repo.id,
        full_name=repo.full_name,
        default_branch=repo.default_branch,
        index_status=repo.index_status,
        index_error=repo.index_error,
        indexed_commit=repo.indexed_commit,
        file_count=file_count,
    )


@router.get("")
def list_repositories(session: SessionDep, principal: PrincipalDep) -> list[RepositoryOut]:
    counts = (
        select(IndexedFile.repository_id, func.count().label("n"))
        .group_by(IndexedFile.repository_id)
        .subquery()
    )
    query = (
        select(Repository, func.coalesce(counts.c.n, 0))
        .outerjoin(counts, counts.c.repository_id == Repository.id)
        .where(Repository.installation_id.is_not(None))
        .order_by(Repository.full_name)
    )
    visible = principal.readable_ids()
    if visible is not None:
        query = query.where(Repository.github_id.in_(visible))
    rows = session.execute(query).all()
    return [_to_out(repo, count) for repo, count in rows]


@router.post("/{repo_id}/reindex", status_code=202)
def reindex(repo_id: int, session: SessionDep, principal: PrincipalDep) -> RepositoryOut:
    repo = session.get(Repository, repo_id)
    if repo is None:
        raise HTTPException(status_code=404, detail="repository not found")
    principal.require(repo, write=True)
    if repo.index_status in ("queued", "indexing"):
        raise HTTPException(status_code=409, detail="indexing already in progress")

    repo.index_status = "queued"
    repo.index_error = None
    session.commit()
    enqueue_index(repo.id)

    count = session.scalar(
        select(func.count()).select_from(IndexedFile).where(IndexedFile.repository_id == repo.id)
    )
    return _to_out(repo, count or 0)
