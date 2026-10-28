from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session

from app.db import engine, get_session
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
