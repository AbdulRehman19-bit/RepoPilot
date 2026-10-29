"""Server-side login sessions. The cookie holds a random token; only its hash is stored."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import LoginSession, User

COOKIE_NAME = "rp_session"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: Session, user: User, access: dict[int, str]) -> str:
    """Start a session and return the cookie value (shown to the browser once)."""
    now = datetime.now(UTC)
    db.execute(delete(LoginSession).where(LoginSession.expires_at < now))  # tidy expired rows
    token = secrets.token_urlsafe(32)
    db.add(
        LoginSession(
            token_hash=_hash(token),
            user_id=user.id,
            login=user.login,
            access={str(repo_id): level for repo_id, level in access.items()},
            expires_at=now + timedelta(hours=settings.session_ttl_hours),
        )
    )
    db.commit()
    return token


def find_session(db: Session, token: str | None) -> LoginSession | None:
    if not token:
        return None
    row = db.scalar(select(LoginSession).where(LoginSession.token_hash == _hash(token)))
    if row is None or row.expires_at <= datetime.now(UTC):
        return None
    return row


def end_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(LoginSession).where(LoginSession.token_hash == _hash(token)))
        db.commit()
