import hmac
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.ratelimit import per_ip
from app.api.security import same_origin
from app.auth import oauth, sessions
from app.config import settings
from app.db import get_session
from app.github.events import upsert_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
STATE_COOKIE = "rp_oauth_state"
STATE_MAX_AGE = 600


def _cookie(response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        path="/",
    )


@router.get("/config")
def auth_config(request: Request, db: Annotated[Session, Depends(get_session)]) -> dict:
    """What the sign-in screen needs. Public: it reveals nothing beyond who you already are."""
    row = sessions.find_session(db, request.cookies.get(sessions.COOKIE_NAME))
    return {
        "login_available": oauth.is_configured(),
        "login": row.login if row else None,
        "token_required": bool(settings.api_token),
        "open": not settings.api_token and not oauth.is_configured(),
    }


@router.get("/login", dependencies=[per_ip("login", "rate_limit_login_per_minute")])
def login() -> RedirectResponse:
    if not oauth.is_configured():
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")
    state = secrets.token_urlsafe(24)
    response = RedirectResponse(oauth.authorize_url(state), status_code=302)
    _cookie(response, STATE_COOKIE, state, STATE_MAX_AGE)
    return response


@router.get("/callback", dependencies=[per_ip("login", "rate_limit_login_per_minute")])
def callback(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
    code: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    if not oauth.is_configured():
        raise HTTPException(status_code=503, detail="GitHub sign-in is not configured")
    expected = request.cookies.get(STATE_COOKIE, "")
    if not (code and state and expected and hmac.compare_digest(state.encode(), expected.encode())):
        raise HTTPException(status_code=400, detail="invalid or expired sign-in attempt")

    try:
        profile = oauth.complete_login(code)
    except oauth.OAuthError as exc:
        logger.warning("sign-in failed: %s", exc)
        raise HTTPException(status_code=502, detail="GitHub sign-in failed; try again") from exc

    user = upsert_user(db, profile.github_id, profile.login)
    db.flush()
    token = sessions.create_session(db, user, profile.access)

    response = RedirectResponse(settings.frontend_url, status_code=302)
    _cookie(response, sessions.COOKIE_NAME, token, settings.session_ttl_hours * 3600)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@router.post("/logout", status_code=204)
def logout(request: Request, db: Annotated[Session, Depends(get_session)]) -> Response:
    token = request.cookies.get(sessions.COOKIE_NAME)
    if token and not same_origin(request):
        raise HTTPException(status_code=403, detail="cross-origin request refused")
    sessions.end_session(db, token)
    response = Response(status_code=204)
    response.delete_cookie(sessions.COOKIE_NAME, path="/")
    return response
