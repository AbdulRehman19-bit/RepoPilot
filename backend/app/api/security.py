"""Who is calling, and which repositories they may touch.

Three kinds of caller:
- `user`: signed in with GitHub; may reach only the repositories GitHub says they can, read or write.
- `admin`: presents the shared REPOPILOT_API_TOKEN (scripts, break-glass); may reach everything.
- `open`: nothing is configured (local development); everything is reachable, but anything that
  writes to GitHub is disabled.
"""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import oauth, sessions
from app.config import settings
from app.db import get_session
from app.models import Repository

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Principal:
    kind: str  # "user" | "admin" | "open"
    login: str | None = None
    access: Mapping[int, str] = field(default_factory=dict)  # GitHub repo id -> read | write

    @property
    def unrestricted(self) -> bool:
        return self.kind != "user"

    def readable_ids(self) -> set[int] | None:
        """GitHub ids of readable repositories, or None when everything is readable."""
        return None if self.unrestricted else set(self.access)

    def require(self, repo: Repository, *, write: bool = False) -> None:
        """404 if the caller can't see the repository (so its existence isn't revealed), 403 if
        they can see it but only have read access to something that needs write access."""
        if self.unrestricted:
            return
        level = self.access.get(repo.github_id)
        if level is None:
            raise HTTPException(status_code=404, detail="not found")
        if write and level != "write":
            raise HTTPException(status_code=403, detail="you need write access to this repository")


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def same_origin(request: Request) -> bool:
    """The request came from our own page: its Origin is this host, or the configured frontend
    (which differs from the Host header when a proxy rewrites it)."""
    origin = request.headers.get("origin")
    if not origin:
        return False
    allowed = {request.headers.get("host"), urlsplit(settings.frontend_url).netloc}
    return urlsplit(origin).netloc in allowed - {None, ""}


def get_principal(request: Request, db: Annotated[Session, Depends(get_session)]) -> Principal:
    tokens = settings.named_tokens()
    bearer = request.headers.get("authorization", "")
    if bearer and tokens:
        supplied = bearer.removeprefix("Bearer ").strip().encode()
        matched: tuple[str, str] | None = None
        for name, token in tokens.items():  # compare against all, so timing says nothing
            if hmac.compare_digest(supplied, token.encode()):
                matched = (name, token)
        if matched is not None:
            name, token = matched
            # Only named tokens say who is calling. The legacy single token is anonymous, so a
            # reviewer name typed into the page is still recorded for it.
            legacy = settings.api_token and name == "admin" and token == settings.api_token
            return Principal("admin", None if legacy else name)

    row = sessions.find_session(db, request.cookies.get(sessions.COOKIE_NAME))
    if row is not None:
        # A cookie is sent automatically by the browser, so a cross-site page could ride on it.
        # SameSite=Lax already blocks that for POSTs; this is the second lock.
        if request.method not in SAFE_METHODS and not same_origin(request):
            raise HTTPException(status_code=403, detail="cross-origin request refused")
        return Principal("user", row.login, {int(k): v for k, v in row.access.items()})

    if not tokens and not oauth.is_configured():
        return Principal("open")
    raise _unauthorized("sign in, or send a valid API token")


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_approvals_enabled(principal: PrincipalDep) -> None:
    """Approving writes to GitHub, so it is never available on an unauthenticated API."""
    if principal.kind == "open":
        raise HTTPException(
            status_code=503,
            detail="approvals are disabled until sign-in or REPOPILOT_API_TOKEN is configured",
        )
