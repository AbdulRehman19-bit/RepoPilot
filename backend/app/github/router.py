import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.github.events import handle_event
from app.github.webhooks import verify_signature
from app.models import Repository
from app.worker.queue import enqueue_index

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/github", tags=["github"])


def _process(session: Session, event: str, payload: dict) -> bool:
    repos = handle_event(session, event, payload)
    if repos is None:
        return False
    session.flush()

    # Index repositories we have not indexed yet (or that failed); skip ones already in flight.
    pending = {r.id: r for r in repos if r.index_status in ("pending", "failed")}
    for repo in pending.values():
        repo.index_status = "queued"
        repo.index_error = None
    session.commit()

    # Enqueue only after the commit so the worker is guaranteed to see the rows.
    for repo_id in pending:
        try:
            enqueue_index(repo_id)
        except Exception:
            logger.exception("could not enqueue indexing for repository %s", repo_id)
            repo = session.get(Repository, repo_id)
            repo.index_status = "failed"
            repo.index_error = "could not enqueue indexing job"
            session.commit()
    return True


@router.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_github_event: Annotated[str | None, Header()] = None,
) -> dict[str, str | bool]:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")
    if x_github_event is None:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Event")

    payload = json.loads(body)
    handled = await run_in_threadpool(_process, session, x_github_event, payload)
    return {"event": x_github_event, "action": str(payload.get("action", "")), "handled": handled}
