from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.db import engine, get_session
from app.github import router as github_router
from app.main import app


@pytest.fixture
def db_session() -> Iterator[Session]:
    """A session whose work is rolled back at the end, even across commit() calls."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
    app.dependency_overrides[get_session] = lambda: session
    try:
        yield session
    finally:
        app.dependency_overrides.pop(get_session, None)
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture(autouse=True)
def no_replay_store(monkeypatch):
    """Webhook tests must not depend on (or leave behind) claims in a real Redis."""
    monkeypatch.setattr(
        github_router, "replay", SimpleNamespace(claim=lambda body: True, release=lambda body: None)
    )
