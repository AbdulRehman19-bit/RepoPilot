import json

from fastapi import APIRouter, Header, HTTPException, Request

from app.config import settings
from app.github.webhooks import verify_signature

router = APIRouter(prefix="/github", tags=["github"])


@router.post("/webhook", status_code=202)
async def webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict[str, str]:
    body = await request.body()
    if not verify_signature(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="invalid signature")
    if x_github_event is None:
        raise HTTPException(status_code=400, detail="missing X-GitHub-Event")

    payload = json.loads(body)
    # Event handling (installation sync, issue ingestion) is added in later steps.
    return {"event": x_github_event, "action": str(payload.get("action", ""))}
