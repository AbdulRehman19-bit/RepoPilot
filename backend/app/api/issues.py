from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.security import Principal, PrincipalDep
from app.db import get_session
from app.llm.base import LLMError
from app.llm.explain import ModelExplanation, explain_with_model
from app.llm.factory import get_provider
from app.models import Issue, Repository
from app.retrieval.context import explain
from app.retrieval.service import Investigation, investigate

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
    explanation: str
    reasons: list[str]
    focus: list[FocusOut]


class ContextBlockOut(BaseModel):
    path: str
    start_line: int
    end_line: int
    label: str
    text: str
    tokens: int
    truncated: bool


class SignalsOut(BaseModel):
    paths: list[str]
    identifiers: list[str]
    error_types: list[str]


class RetrievalOut(BaseModel):
    issue: IssueOut
    signals: SignalsOut
    matches: list[MatchOut]
    context: list[ContextBlockOut]
    context_tokens: int


class ExplainOut(RetrievalOut):
    """Retrieval plus an optional model-written analysis. Works without a model."""

    model_used: bool
    model_name: str | None
    model_error: str | None
    analysis: ModelExplanation | None


def _issue_out(issue: Issue) -> IssueOut:
    return IssueOut(
        id=issue.id,
        repository=issue.repository.full_name,
        number=issue.number,
        title=issue.title,
    )


def _investigate(
    session: Session, principal: Principal, issue_id: int, limit: int, budget_tokens: int
) -> Investigation:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    principal.require(issue.repository)
    if issue.repository.index_status != "ready":
        raise HTTPException(status_code=409, detail="repository has not been indexed yet")
    return investigate(session, issue, limit, budget_tokens)


def _retrieval_out(found: Investigation) -> dict:
    return {
        "issue": _issue_out(found.issue),
        "signals": SignalsOut(
            paths=[ref.path + (f":{ref.line}" if ref.line else "") for ref in found.signals.paths],
            identifiers=sorted(found.signals.identifiers),
            error_types=sorted(found.signals.error_types),
        ),
        "matches": [
            MatchOut(
                path=m.path,
                score=m.score,
                explanation=explain(m),
                reasons=m.reasons,
                focus=[FocusOut(**vars(f)) for f in m.focus],
            )
            for m in found.matches
        ],
        "context": [ContextBlockOut(**vars(b)) for b in found.blocks],
        "context_tokens": sum(b.tokens for b in found.blocks),
    }


@router.get("")
def list_issues(session: SessionDep, principal: PrincipalDep) -> list[IssueOut]:
    query = select(Issue).order_by(Issue.updated_at.desc()).limit(100)
    visible = principal.readable_ids()
    if visible is not None:
        query = query.join(Repository).where(Repository.github_id.in_(visible))
    issues = session.scalars(query)
    return [_issue_out(issue) for issue in issues]


@router.post("/{issue_id}/retrieve")
def retrieve(
    issue_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=25)] = 8,
    budget_tokens: Annotated[int, Query(ge=500, le=16000)] = 3000,
) -> RetrievalOut:
    return RetrievalOut(
        **_retrieval_out(_investigate(session, principal, issue_id, limit, budget_tokens))
    )


@router.post("/{issue_id}/explain")
def explain_issue(
    issue_id: int,
    session: SessionDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=25)] = 6,
    budget_tokens: Annotated[int, Query(ge=500, le=8000)] = 2500,
) -> ExplainOut:
    """Retrieval plus a model-written analysis; falls back to retrieval alone if no model works."""
    found = _investigate(session, principal, issue_id, limit, budget_tokens)
    result = ExplainOut(
        **_retrieval_out(found),
        model_used=False,
        model_name=None,
        model_error=None,
        analysis=None,
    )
    if not found.matches:
        result.model_error = "no relevant files were found to analyse"
        return result

    provider = get_provider()
    if provider is None:
        result.model_error = "the model is disabled (REPOPILOT_LLM_PROVIDER=none)"
        return result
    ok, reason = provider.available()
    if not ok:
        result.model_error = reason
        return result

    try:
        result.analysis = explain_with_model(
            provider, found.issue.title, found.issue.body, found.matches, found.blocks
        )
    except LLMError as exc:
        result.model_error = str(exc)
        return result
    result.model_used = True
    result.model_name = provider.model
    return result
