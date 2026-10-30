"""End login sessions from the command line, e.g. when someone loses access on GitHub.

    uv run python -m app.auth.revoke alice     # all of alice's sessions
    uv run python -m app.auth.revoke --all     # everyone must sign in again

Permissions are read from GitHub at sign-in and kept for the session, so this is how to make a
change take effect before the session expires.
"""

import sys

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import LoginSession


def revoke(login: str | None, db: Session | None = None) -> int:
    """Delete one user's sessions (or all of them when `login` is None). Returns how many."""
    statement = delete(LoginSession)
    if login is not None:
        statement = statement.where(LoginSession.login == login)
    if db is not None:
        removed = db.execute(statement).rowcount
        db.commit()
        return removed
    with SessionLocal() as own:
        return revoke(login, own)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    removed = revoke(None if argv[0] == "--all" else argv[0])
    print(f"ended {removed} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
