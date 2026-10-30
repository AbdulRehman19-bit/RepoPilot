from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.ratelimit import per_caller
from app.api.security import Principal, PrincipalDep, require_approvals_enabled
from app.config import settings
from app.db import get_session
from app.github.pulls import (
    GitHubError,
    PatchMismatch,
    StaleBase,
    build_pr_body,
    get_github_client,
    open_draft_pr,
)
from app.llm.factory import get_provider
from app.models import Issue, Patch, Repository, TestExecution
from app.worker.queue import enqueue_patch

router = APIRouter(tags=["patches"])

SessionDep = Annotated[Session, Depends(get_session)]
ACTIVE = ("queued", "running")
OUTPUT_TAIL_CHARS = 1500


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
    # What a reviewer needs to decide: the failure before, and the results after.
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    review_status: str | None
    reviewer: str | None
    review_note: str | None
    branch_name: str | None
    pr_number: int | None
    pr_url: str | None
    pr_error: str | None


class PatchIn(BaseModel):
    reproduction_id: int | None = None


class ReviewIn(BaseModel):
    reviewer: str | None = Field(default=None, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


def _out(patch: Patch, session: Session) -> PatchOut:
    reproduction = (
        session.get(TestExecution, patch.reproduction_id) if patch.reproduction_id else None
    )
    before = None
    if reproduction is not None:
        before = {
            "selectors": reproduction.selectors,
            "failing": [f["node_id"] for f in reproduction.failures or []],
            "output": (reproduction.output or "")[-OUTPUT_TAIL_CHARS:],
        }
    verified = next((a for a in patch.attempts or [] if a.get("outcome") == "verified"), None)
    after = (
        {"targeted": verified.get("targeted"), "full": verified.get("full")} if verified else None
    )

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
        before=before,
        after=after,
        review_status=patch.review_status,
        reviewer=patch.reviewer,
        review_note=patch.review_note,
        branch_name=patch.branch_name,
        pr_number=patch.pr_number,
        pr_url=patch.pr_url,
        pr_error=patch.pr_error,
    )


def _reproduced(execution: TestExecution) -> bool:
    return (
        execution.kind == "reproduction"
        and execution.status == "completed"
        and execution.exit_code not in (0, 5, None)
        and not execution.timed_out
    )


@router.post(
    "/issues/{issue_id}/patch",
    status_code=202,
    dependencies=[per_caller("jobs", "rate_limit_jobs_per_hour")],
)
def start_patch(
    issue_id: int, session: SessionDep, principal: PrincipalDep, body: PatchIn | None = None
) -> PatchOut:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    repo = issue.repository
    principal.require(repo, write=True)
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
        issue_id=issue.id,
        repository_id=repo.id,
        reproduction_id=reproduction.id,
        status="queued",
        requested_by=principal.login,
    )
    session.add(patch)
    session.commit()
    try:
        enqueue_patch(patch.id)
    except Exception as exc:  # noqa: BLE001 - surface any queue failure on the record
        patch.status, patch.error = "error", f"could not enqueue patch job: {exc}"
        session.commit()
    return _out(patch, session)


@router.get("/patches/{patch_id}")
def get_patch(patch_id: int, session: SessionDep, principal: PrincipalDep) -> PatchOut:
    patch = session.get(Patch, patch_id)
    if patch is None:
        raise HTTPException(status_code=404, detail="patch not found")
    principal.require(session.get(Repository, patch.repository_id))
    return _out(patch, session)


@router.get("/issues/{issue_id}/patches")
def list_patches(issue_id: int, session: SessionDep, principal: PrincipalDep) -> list[PatchOut]:
    issue = session.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="issue not found")
    principal.require(issue.repository)
    rows = session.scalars(
        select(Patch).where(Patch.issue_id == issue_id).order_by(Patch.id.desc()).limit(20)
    )
    return [_out(row, session) for row in rows]


# --- Human review and pull request creation -----------------------------------------------------


def _locked_patch(session: Session, principal: Principal, patch_id: int) -> Patch:
    # Row lock: two reviewers clicking at once must not open two pull requests.
    patch = session.get(Patch, patch_id, with_for_update=True)
    if patch is None:
        raise HTTPException(status_code=404, detail="patch not found")
    principal.require(session.get(Repository, patch.repository_id), write=True)
    return patch


def _reviewer(principal: Principal, body: "ReviewIn | None") -> str | None:
    """A signed-in reviewer is who GitHub says they are; a typed-in name is only for tokens."""
    return principal.login or (body.reviewer if body else None)


def _open_pull_request(session: Session, patch: Patch) -> PatchOut:
    repo = session.get(Repository, patch.repository_id)
    issue = session.get(Issue, patch.issue_id)
    reproduction = session.get(TestExecution, patch.reproduction_id)

    def fail(message: str, status: int) -> HTTPException:
        patch.pr_error = message
        session.commit()
        return HTTPException(status_code=status, detail=message)

    if not repo.installation_id:
        raise fail("the repository is not connected to the GitHub App", 409)
    if not patch.commit_sha or not patch.diff:
        raise fail("the patch has no recorded base commit or diff", 409)

    try:
        gh = get_github_client(repo.installation_id)
        pr = open_draft_pr(
            gh,
            full_name=repo.full_name,
            base_branch=repo.default_branch,
            base_sha=patch.commit_sha,
            edits=patch.edits,
            verified_diff=patch.diff,
            protected={s.split("::")[0] for s in reproduction.selectors},
            issue_number=issue.number,
            patch_id=patch.id,
            title=f"Fix #{issue.number}: {issue.title}"[:120],
            body=build_pr_body(
                issue_number=issue.number,
                issue_title=issue.title,
                explanation=patch.explanation or "",
                attempts=patch.attempts or [],
                before_failing=[f["node_id"] for f in reproduction.failures or []],
                reviewer=patch.reviewer,
                note=patch.review_note,
            ),
        )
    except (StaleBase, PatchMismatch) as exc:
        raise fail(str(exc), 409) from exc
    except (GitHubError, httpx.HTTPError, RuntimeError, OSError) as exc:
        raise fail(f"could not open the pull request: {exc}", 502) from exc

    patch.pr_url, patch.pr_number, patch.branch_name, patch.pr_error = (
        pr.url,
        pr.number,
        pr.branch,
        None,
    )
    session.commit()
    return _out(patch, session)


@router.post("/patches/{patch_id}/approve", dependencies=[Depends(require_approvals_enabled)])
def approve(
    patch_id: int, session: SessionDep, principal: PrincipalDep, body: ReviewIn | None = None
) -> PatchOut:
    """Approve a verified patch and open a draft pull request for it."""
    patch = _locked_patch(session, principal, patch_id)
    if patch.status != "verified":
        raise HTTPException(status_code=409, detail="only verified patches can be approved")
    if patch.review_status == "rejected":
        raise HTTPException(status_code=409, detail="this patch was rejected")
    if patch.pr_url:
        raise HTTPException(status_code=409, detail="a pull request was already opened")

    if patch.review_status is None:
        reviewer = _reviewer(principal, body)
        if (
            settings.require_separate_reviewer
            and patch.requested_by
            and reviewer == patch.requested_by
        ):
            raise HTTPException(
                status_code=403, detail="a different person than the requester must approve"
            )
        patch.review_status = "approved"
        patch.reviewer = reviewer
        patch.review_note = body.note if body else None
        patch.reviewed_at = datetime.now(UTC)
        session.commit()  # keep the decision even if GitHub fails; retry with /open-pr
        patch = _locked_patch(session, principal, patch_id)
    return _open_pull_request(session, patch)


@router.post("/patches/{patch_id}/open-pr", dependencies=[Depends(require_approvals_enabled)])
def retry_open_pr(patch_id: int, session: SessionDep, principal: PrincipalDep) -> PatchOut:
    """Retry opening the pull request for an already-approved patch."""
    patch = _locked_patch(session, principal, patch_id)
    if patch.review_status != "approved":
        raise HTTPException(status_code=409, detail="the patch has not been approved")
    if patch.pr_url:
        raise HTTPException(status_code=409, detail="a pull request was already opened")
    return _open_pull_request(session, patch)


@router.post("/patches/{patch_id}/reject", dependencies=[Depends(require_approvals_enabled)])
def reject(
    patch_id: int, session: SessionDep, principal: PrincipalDep, body: ReviewIn | None = None
) -> PatchOut:
    patch = _locked_patch(session, principal, patch_id)
    if patch.status != "verified":
        raise HTTPException(status_code=409, detail="only verified patches can be reviewed")
    if patch.review_status is not None:
        raise HTTPException(status_code=409, detail=f"this patch was already {patch.review_status}")

    patch.review_status = "rejected"
    patch.reviewer = _reviewer(principal, body)
    patch.review_note = body.note if body else None
    patch.reviewed_at = datetime.now(UTC)
    session.commit()
    return _out(patch, session)
