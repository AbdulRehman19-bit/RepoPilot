import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Issue, Repository, User

SECRET = "test-secret"
INSTALLATION = {"id": 777_001, "account": {"id": 777_100, "login": "octo"}}
REPO = {"id": 777_200, "full_name": "octo/demo", "name": "demo"}


def post(client: TestClient, event: str, payload: dict):
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/github/webhook",
        content=body,
        headers={"X-GitHub-Event": event, "X-Hub-Signature-256": signature},
    )


def issue_payload(number: int = 1, title: str = "Crash", body: str | None = "Boom", **extra):
    return {
        "action": "opened",
        "installation": {"id": INSTALLATION["id"]},
        "issue": {"number": number, "title": title, "body": body},
        "repository": {
            **REPO,
            "default_branch": "develop",
            "owner": {"id": 777_100, "login": "octo"},
        },
        **extra,
    }


@pytest.fixture
def client(monkeypatch, db_session):
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    return TestClient(app)


def test_unhandled_event_is_acknowledged(client):
    response = post(client, "ping", {"zen": "hi"})
    assert response.status_code == 202
    assert response.json()["handled"] is False


def test_installation_created_stores_repositories(client, db_session):
    payload = {"action": "created", "installation": INSTALLATION, "repositories": [REPO]}
    assert post(client, "installation", payload).json()["handled"] is True

    repo = db_session.scalar(select(Repository).where(Repository.github_id == REPO["id"]))
    assert repo.full_name == "octo/demo"
    assert repo.installation_id == INSTALLATION["id"]
    assert repo.owner.login == "octo"

    # Replaying the same event must not duplicate rows.
    post(client, "installation", payload)
    assert len(db_session.scalars(select(User).where(User.github_id == 777_100)).all()) == 1
    assert (
        len(db_session.scalars(select(Repository).where(Repository.github_id == 777_200)).all())
        == 1
    )


def test_installation_repositories_add_and_remove(client, db_session):
    added = {
        "action": "added",
        "installation": INSTALLATION,
        "repositories_added": [REPO],
        "repositories_removed": [],
    }
    post(client, "installation_repositories", added)
    repo = db_session.scalar(select(Repository).where(Repository.github_id == REPO["id"]))
    assert repo.installation_id == INSTALLATION["id"]

    removed = {
        "action": "removed",
        "installation": INSTALLATION,
        "repositories_added": [],
        "repositories_removed": [REPO],
    }
    post(client, "installation_repositories", removed)
    db_session.refresh(repo)
    assert repo.installation_id is None


def test_installation_deleted_disconnects_repositories(client, db_session):
    post(
        client,
        "installation",
        {"action": "created", "installation": INSTALLATION, "repositories": [REPO]},
    )
    post(client, "installation", {"action": "deleted", "installation": INSTALLATION})
    repo = db_session.scalar(select(Repository).where(Repository.github_id == REPO["id"]))
    assert repo.installation_id is None


def test_issue_opened_then_edited(client, db_session):
    assert post(client, "issues", issue_payload()).json()["handled"] is True
    issue = db_session.scalar(select(Issue).where(Issue.title == "Crash"))
    assert issue.number == 1
    assert issue.body == "Boom"
    assert issue.repository.default_branch == "develop"

    post(client, "issues", issue_payload(title="Crash on start", body=None, action="edited"))
    db_session.refresh(issue)
    assert issue.title == "Crash on start"
    assert issue.body == ""
    assert (
        len(
            db_session.scalars(
                select(Issue).where(Issue.repository_id == issue.repository_id)
            ).all()
        )
        == 1
    )


def test_ignored_issue_action(client):
    response = post(client, "issues", issue_payload(action="labeled"))
    assert response.json()["handled"] is False
