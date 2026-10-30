import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.auth import oauth, revoke, sessions
from app.config import settings
from app.llm.fake import FakeProvider
from app.main import app
from app.models import Issue, LoginSession, Patch, Repository, TestExecution, User
from app.security.redact import redact

TOKENS = "alice:alice-secret-token, ci:ci-secret:with:colons , broken, :nameless, empty:"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- Named API tokens ----------------------------------------------------------------------------


def test_named_tokens_parse_and_ignore_malformed_entries(monkeypatch):
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    monkeypatch.setattr(settings, "api_token", "legacy-shared-token")
    assert settings.named_tokens() == {
        "alice": "alice-secret-token",
        "ci": "ci-secret:with:colons",  # only the first colon separates the name
        "admin": "legacy-shared-token",
    }


def test_the_legacy_single_token_never_overrides_a_named_admin(monkeypatch):
    monkeypatch.setattr(settings, "api_tokens", "admin:named-wins")
    monkeypatch.setattr(settings, "api_token", "legacy-shared-token")
    assert settings.named_tokens() == {"admin": "named-wins"}


@pytest.fixture
def client(db_session, monkeypatch):
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    monkeypatch.setattr(settings, "api_token", "")

    def no_github(installation_id):
        raise RuntimeError("tests never talk to GitHub")

    monkeypatch.setattr("app.api.patches.get_github_client", no_github)
    return TestClient(app, follow_redirects=False)


def test_each_token_identifies_its_holder(client):
    assert client.get("/repositories").status_code == 401
    for token in ("alice-secret-token", "ci-secret:with:colons"):
        assert client.get("/repositories", headers=bearer(token)).status_code == 200
    assert client.get("/repositories", headers=bearer("alice-secret")).status_code == 401
    assert client.get("/repositories", headers=bearer("")).status_code == 401


def test_every_token_is_redacted_from_output(monkeypatch):
    monkeypatch.setattr(settings, "api_tokens", TOKENS)
    cleaned = redact("leak alice-secret-token and ci-secret:with:colons here")
    assert "alice-secret-token" not in cleaned and "ci-secret:with:colons" not in cleaned


def test_auth_config_reports_tokens_without_revealing_them(client):
    body = client.get("/auth/config").json()
    assert body["token_required"] is True and body["open"] is False
    assert "secret" not in str(body)


# --- Who asked, who approved ---------------------------------------------------------------------


@pytest.fixture
def patch_row(db_session):
    repo = Repository(
        owner=User(github_id=31_001, login="o"),
        github_id=31_002,
        full_name="o/r",
        installation_id=7,
        index_status="ready",
        default_branch="main",
    )
    issue = Issue(repository=repo, number=1, title="bug", body="")
    db_session.add_all([repo, issue])
    db_session.flush()
    execution = TestExecution(
        repository_id=repo.id,
        issue_id=issue.id,
        kind="reproduction",
        status="completed",
        exit_code=1,
        selectors=["test_x.py::test_y"],
        failures=[],
    )
    db_session.add(execution)
    db_session.flush()
    patch = Patch(
        issue_id=issue.id,
        repository_id=repo.id,
        reproduction_id=execution.id,
        status="verified",
        diff="d",
        edits=[],
        attempts=[],
        commit_sha="a" * 40,
        requested_by="alice",
    )
    db_session.add(patch)
    db_session.flush()
    return patch


def test_starting_a_patch_records_who_asked(client, db_session, patch_row, monkeypatch):
    monkeypatch.setattr("app.api.patches.enqueue_patch", lambda patch_id: None)
    monkeypatch.setattr("app.api.patches.get_provider", lambda: FakeProvider())
    issue_id = patch_row.issue_id
    db_session.delete(patch_row)
    db_session.flush()

    response = client.post(f"/issues/{issue_id}/patch", headers=bearer("ci-secret:with:colons"))
    assert response.status_code == 202
    created = db_session.get(Patch, response.json()["id"])
    assert created.requested_by == "ci"


def test_requester_cannot_approve_when_the_rule_is_on(client, db_session, patch_row, monkeypatch):
    monkeypatch.setattr(settings, "require_separate_reviewer", True)
    own = client.post(f"/patches/{patch_row.id}/approve", headers=bearer("alice-secret-token"))
    assert own.status_code == 403 and "different person" in own.json()["detail"]
    assert db_session.get(Patch, patch_row.id).review_status is None  # nothing was recorded

    other = client.post(f"/patches/{patch_row.id}/approve", headers=bearer("ci-secret:with:colons"))
    assert other.status_code != 403  # got past the rule (GitHub itself is not configured here)
    row = db_session.get(Patch, patch_row.id)
    assert (row.review_status, row.reviewer) == ("approved", "ci")


def test_requester_may_approve_when_the_rule_is_off(client, db_session, patch_row):
    assert settings.require_separate_reviewer is False
    response = client.post(f"/patches/{patch_row.id}/approve", headers=bearer("alice-secret-token"))
    assert response.status_code != 403
    assert db_session.get(Patch, patch_row.id).reviewer == "alice"


# --- Origin behind a proxy -----------------------------------------------------------------------


def test_the_configured_frontend_origin_is_accepted_when_a_proxy_rewrites_host(
    client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    user = User(github_id=1, login="alice")
    db_session.add(user)
    db_session.flush()
    client.cookies.set("rp_session", sessions.create_session(db_session, user, {}))

    def post(origin):
        headers = {"Host": "api.internal:8000", "Origin": origin}
        return client.post("/issues/1/patch", headers=headers).status_code

    assert post("https://app.example.com") == 404  # allowed through to the (missing) issue
    assert post("http://api.internal:8000") == 404  # the Host itself still works
    assert post("https://evil.example.com") == 403


# --- Sign-in race and revocation -----------------------------------------------------------------


def test_simultaneous_first_sign_ins_do_not_crash(db_session, monkeypatch):
    from app.auth import router as auth_router

    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    profile = oauth.Profile(github_id=909, login="newbie", access={})
    monkeypatch.setattr(oauth, "complete_login", lambda code: profile)
    real_upsert = auth_router.upsert_user
    calls = []

    def racing_upsert(db, github_id, login):
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return real_upsert(db, github_id, login)

    monkeypatch.setattr(auth_router, "upsert_user", racing_upsert)
    http = TestClient(app, follow_redirects=False)
    http.cookies.set("rp_oauth_state", "abc")
    response = http.get("/auth/callback?code=c&state=abc")
    assert response.status_code == 302 and len(calls) == 2
    assert db_session.scalar(select(LoginSession).where(LoginSession.login == "newbie"))


def test_revoke_ends_one_users_sessions_or_everyones(db_session):
    users = []
    for gid, login in ((1, "alice"), (2, "bob")):
        user = User(github_id=gid, login=login)
        db_session.add(user)
        db_session.flush()
        users.append(user)
    for user in users:
        sessions.create_session(db_session, user, {})
        sessions.create_session(db_session, user, {})

    assert revoke.revoke("alice", db_session) == 2
    assert {s.login for s in db_session.scalars(select(LoginSession))} == {"bob"}
    assert revoke.revoke(None, db_session) == 2
    assert db_session.scalars(select(LoginSession)).all() == []


def test_revoke_command_prints_usage_without_touching_anything(capsys):
    assert revoke.main([]) == 2
    assert "revoke" in capsys.readouterr().out


def test_the_legacy_single_token_stays_anonymous_so_a_typed_reviewer_name_is_kept(
    db_session, patch_row, monkeypatch
):
    monkeypatch.setattr(settings, "api_tokens", "")
    monkeypatch.setattr(settings, "api_token", "legacy-shared-token")
    monkeypatch.setattr(
        "app.api.patches.get_github_client",
        lambda installation_id: (_ for _ in ()).throw(RuntimeError("no github")),
    )
    http = TestClient(app, follow_redirects=False)
    http.post(
        f"/patches/{patch_row.id}/approve",
        headers=bearer("legacy-shared-token"),
        json={"reviewer": "Sam"},
    )
    assert db_session.get(Patch, patch_row.id).reviewer == "Sam"


def test_a_named_token_cannot_be_overridden_by_a_typed_name(client, db_session, patch_row):
    client.post(
        f"/patches/{patch_row.id}/approve",
        headers=bearer("ci-secret:with:colons"),
        json={"reviewer": "someone-else"},
    )
    assert db_session.get(Patch, patch_row.id).reviewer == "ci"
