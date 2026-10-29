import hmac
from typing import Annotated

from fastapi import Header, HTTPException

from app.config import settings


def verify_token(authorization: Annotated[str | None, Header()] = None) -> None:
    """Require `Authorization: Bearer <REPOPILOT_API_TOKEN>` whenever a token is configured."""
    if not settings.api_token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), settings.api_token.encode()):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def require_approvals_enabled() -> None:
    """Approving writes to GitHub, so it is never available on an unauthenticated API."""
    if not settings.api_token:
        raise HTTPException(
            status_code=503,
            detail="approvals are disabled until REPOPILOT_API_TOKEN is set",
        )
