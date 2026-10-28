from sqlalchemy import select

from app.db import SessionLocal
from app.models import Issue, Repository, Run, RunStatus, User


def test_run_relationships_roundtrip():
    with SessionLocal() as session, session.begin():
        user = User(github_id=999_001, login="tester")
        repo = Repository(owner=user, github_id=999_002, full_name="tester/demo")
        issue = Issue(repository=repo, number=1, title="Bug", body="It breaks")
        run = Run(issue=issue)
        session.add(run)
        session.flush()

        loaded = session.scalar(select(Run).where(Run.id == run.id))
        assert loaded.status == RunStatus.queued
        assert loaded.issue.repository.full_name == "tester/demo"

        session.rollback()
