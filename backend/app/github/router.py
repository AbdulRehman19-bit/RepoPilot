import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.github.events import handle_event
from app.github.webhooks import verify_signature

router = APIRouter(prefix="/github", tags=["github"])


def _process(session: Session, event: str, payload: dict) -> bool:
    handled = handle_event(session, event, payload)
    session.commit()
    return handled


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
