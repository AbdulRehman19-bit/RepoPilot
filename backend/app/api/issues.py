from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session
from app.models import Issue
from app.retrieval.ranker import rank_files
from app.retrieval.signals import extract_signals

router = APIRouter(prefix="/issues", tags=["issues"])

SessionDep = Annotated[Session, Depends(get_session)]


class IssueOut(BaseModel):
    id: int
    repository: str
    number: int
    title: str


class FocusOut(BaseModel):
    label: str
    start_line: int
    end_line: int


class MatchOut(BaseModel):
    path: str
    score: float
    reasons: list[str]
    focus: list[FocusOut]


class SignalsOut(BaseModel):
    paths: list[str]
    identifiers: list[str]
    error_types: list[str]


class RetrievalOut(BaseModel):
    issue: IssueOut
    signals: SignalsOut
    matches: list[MatchOut]


def _issue_out(issue: Issue) -> IssueOut:
    return IssueOut(
        id=issue.id,
        repository=issue.repository.full_name,
        number=issue.number,
        title=issue.title,
    )


@router.get("")
def list_issues(session: SessionDep) -> list[IssueOut]:
    issues = session.scalars(select(Issue).order_by(Issue.updated_at.desc()).limit(100))
    return [_issue_out(issue) for issue in issues]


@router.post("/{issue_id}/retrieve")
def retrieve(
    issue_id: int, session: SessionDep, limit: Annotated[int, Query(ge=1, le=25)] = 8
) -> RetrievalOut:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    if issue.repository.index_status != "ready":
        raise HTTPException(status_code=409, detail="repository has not been indexed yet")

    text = f"{issue.title}\n{issue.body}"
    signals = extract_signals(text)
    matches = rank_files(session, issue.repository_id, text, limit)
    return RetrievalOut(
        issue=_issue_out(issue),
        signals=SignalsOut(
            paths=[ref.path + (f":{ref.line}" if ref.line else "") for ref in signals.paths],
            identifiers=sorted(signals.identifiers),
            error_types=sorted(signals.error_types),
        ),
        matches=[
            MatchOut(
                path=m.path,
                score=m.score,
                reasons=m.reasons,
                focus=[FocusOut(**vars(f)) for f in m.focus],
            )
            for m in matches
        ],
    )
