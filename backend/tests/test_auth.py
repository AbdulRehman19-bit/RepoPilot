from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth import oauth, sessions
from app.auth.oauth import OAuthError, complete_login
from app.config import settings
from app.main import app
from app.models import Issue, LoginSession, Patch, Repository, TestExecution, User

ORIGIN = {"Origin": "http://testserver"}


@pytest.fixture(autouse=True)
def oauth_app(monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "client-id")
    monkeypatch.setattr(settings, "github_client_secret", "client-secret-value")
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(settings, "session_cookie_secure", False)


# --- GitHub exchange -----------------------------------------------------------------------------


class FakeGitHubUser:
    """OAuth + REST endpoints as GitHub answers them to a user token."""

    def __init__(self, installations, repos_by_installation, token="user-token", code="good"):
        self.installations = installations
        self.repos = repos_by_installation
        self.token, self.code = token, code
        self.seen_tokens: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/login/oauth/access_token":
            form = dict(x.split("=") for x in request.content.decode().split("&"))
            if form["code"] != self.code or form["client_secret"] != "client-secret-value":
                return httpx.Response(200, json={"error": "bad_verification_code"})
            return httpx.Response(200, json={"access_token": self.token})
        self.seen_tokens.append(request.headers["Authorization"])
        page = int(request.url.params.get("page", 1))
        if path == "/user":
            return httpx.Response(200, json={"id": 501, "login": "alice"})
        if path == "/user/installations":
            return httpx.Response(
                200, json={"installations": self.installations if page == 1 else []}
            )
        if path.startswith("/user/installations/") and path.endswith("/repositories"):
            iid = int(path.split("/")[3])
            return httpx.Response(200, json={"repositories": self.repos[iid] if page == 1 else []})
        return httpx.Response(404, json={})

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


def repo(rid, **permissions):
    return {"id": rid, "permissions": permissions}


def test_login_collects_who_the_user_is_and_what_they_can_reach():
    gh = FakeGitHubUser(
        installations=[{"id": 1}, {"id": 2}],
        repos_by_installation={
            1: [repo(10, pull=True), repo(11, pull=True, push=True), repo(12, admin=True)],
            2: [repo(10, pull=True, maintain=True), repo(13, triage=True)],
        },
    )
    profile = complete_login("good", client=gh.client())
    assert (profile.github_id, profile.login) == (501, "alice")
    # Highest permission wins across installations; triage/read count as read-only.
    assert profile.access == {10: "write", 11: "write", 12: "write", 13: "read"}
    assert set(gh.seen_tokens) == {"Bearer user-token"}


def test_login_pages_through_many_repositories(monkeypatch):
    monkeypatch.setattr(oauth, "PAGE_SIZE", 2)
    pages = {1: [repo(1, push=True), repo(2, pull=True)], 2: [repo(3, pull=True)]}

    def handle(request):
        path = request.url.path
        if path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "t"})
        if path == "/user":
            return httpx.Response(200, json={"id": 1, "login": "a"})
        if path == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": 9}]})
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"repositories": pages.get(page, [])})

    profile = complete_login("c", client=httpx.Client(transport=httpx.MockTransport(handle)))
    assert profile.access == {1: "write", 2: "read", 3: "read"}


def test_bad_code_and_github_errors_fail_cleanly():
    gh = FakeGitHubUser([], {})
    with pytest.raises(OAuthError, match="bad_verification_code|did not return"):
        complete_login("wrong", client=gh.client())

    def down(request):
        if request.url.path == "/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(500)

    with pytest.raises(OAuthError, match="500"):
        complete_login("c", client=httpx.Client(transport=httpx.MockTransport(down)))


# --- Sessions ------------------------------------------------------------------------------------


def make_user(db, github_id=501, login="alice"):
    user = User(github_id=github_id, login=login)
    db.add(user)
    db.flush()
    return user


def test_session_cookie_is_random_and_only_its_hash_is_stored(db_session):
    user = make_user(db_session)
    token = sessions.create_session(db_session, user, {10: "write"})
    row = db_session.scalar(select(LoginSession))
    assert token not in (row.token_hash, row.login) and len(token) >= 40
    assert row.token_hash == sessions._hash(token)
    assert row.access == {"10": "write"}
    assert sessions.find_session(db_session, token).id == row.id
    assert sessions.find_session(db_session, token + "x") is None
    assert sessions.find_session(db_session, None) is None


def test_expired_sessions_are_not_found_and_get_tidied(db_session):
    user = make_user(db_session)
    token = sessions.create_session(db_session, user, {})
    row = db_session.scalar(select(LoginSession))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()
    assert sessions.find_session(db_session, token) is None
    sessions.create_session(db_session, user, {})
    assert db_session.scalars(select(LoginSession)).all().__len__() == 1


# --- Sign-in endpoints ---------------------------------------------------------------------------


@pytest.fixture
def client(db_session):
    return TestClient(app, follow_redirects=False)


def test_login_redirects_to_github_with_a_fresh_state(client):
    first = client.get("/auth/login")
    assert first.status_code == 302
    location = first.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=client-id" in location and "client-secret" not in location
    state = client.cookies["rp_oauth_state"]
    assert f"state={state}" in location
    assert "httponly" in first.headers["set-cookie"].lower()
    assert (
        state
        != TestClient(app).get("/auth/login", follow_redirects=False).cookies["rp_oauth_state"]
    )


def test_login_is_unavailable_when_not_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "")
    assert client.get("/auth/login").status_code == 503
    assert client.get("/auth/callback?code=x&state=y").status_code == 503
    assert client.get("/auth/config").json()["login_available"] is False


@pytest.mark.parametrize("query", ["", "?code=good", "?state=abc", "?code=good&state=other"])
def test_callback_rejects_missing_or_mismatched_state(client, query):
    client.cookies.set("rp_oauth_state", "abc")
    assert client.get(f"/auth/callback{query}").status_code == 400
    assert not client.cookies.get("rp_session")


def test_callback_without_the_state_cookie_is_rejected(client):
    assert client.get("/auth/callback?code=good&state=abc").status_code == 400


def test_successful_sign_in_creates_a_session_and_user(client, db_session, monkeypatch):
    gh = FakeGitHubUser([{"id": 1}], {1: [repo(10, push=True), repo(11, pull=True)]})
    monkeypatch.setattr(oauth, "make_client", gh.client)
    client.cookies.set("rp_oauth_state", "abc")

    response = client.get("/auth/callback?code=good&state=abc")
    assert response.status_code == 302 and response.headers["location"] == settings.frontend_url
    cookie = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookie if c.startswith("rp_session="))
    assert "httponly" in session_cookie.lower() and "samesite=lax" in session_cookie.lower()

    row = db_session.scalar(select(LoginSession))
    assert row.login == "alice" and row.access == {"10": "write", "11": "read"}
    assert db_session.scalar(select(User).where(User.github_id == 501)).login == "alice"
    me = client.get("/auth/config").json()
    assert me["login"] == "alice" and me["login_available"] is True


def test_failed_exchange_does_not_leak_details(client, monkeypatch):
    gh = FakeGitHubUser([], {}, code="other")
    monkeypatch.setattr(oauth, "make_client", gh.client)
    client.cookies.set("rp_oauth_state", "abc")
    response = client.get("/auth/callback?code=good&state=abc")
    assert response.status_code == 502
    assert "client-secret-value" not in response.text and "good" not in response.text


def test_logout_ends_the_session(client, db_session):
    token = sessions.create_session(db_session, make_user(db_session), {})
    client.cookies.set("rp_session", token)
    assert client.post("/auth/logout", headers=ORIGIN).status_code == 204
    assert sessions.find_session(db_session, token) is None


def test_cross_origin_logout_is_refused_and_keeps_the_session(client, db_session):
    token = sessions.create_session(db_session, make_user(db_session), {})
    client.cookies.set("rp_session", token)
    assert client.post("/auth/logout", headers={"Origin": "http://evil.test"}).status_code == 403
    assert sessions.find_session(db_session, token) is not None


# --- Who is calling ------------------------------------------------------------------------------


def test_requests_without_credentials_are_refused_once_login_is_configured(client):
    for method, path in [("get", "/repositories"), ("get", "/issues"), ("post", "/issues/1/patch")]:
        assert getattr(client, method)(path).status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/auth/config").status_code == 200


def test_expired_or_bogus_cookie_is_refused(client, db_session):
    client.cookies.set("rp_session", "not-a-real-session")
    assert client.get("/repositories").status_code == 401
    token = sessions.create_session(db_session, make_user(db_session), {})
    db_session.scalar(select(LoginSession)).expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db_session.flush()
    client.cookies.set("rp_session", token)
    assert client.get("/repositories").status_code == 401


def test_api_token_still_works_as_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "api_token", "shared-token-123")
    assert client.get("/repositories").status_code == 401
    ok = client.get("/repositories", headers={"Authorization": "Bearer shared-token-123"})
    assert ok.status_code == 200
    bad = client.get("/repositories", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


def test_cookie_requests_that_change_things_must_come_from_our_own_origin(client, db_session):
    token = sessions.create_session(db_session, make_user(db_session), {})
    client.cookies.set("rp_session", token)
    assert client.get("/repositories").status_code == 200  # reads need no origin
    assert client.post("/issues/1/patch").status_code == 403  # no Origin header
    cross = client.post("/issues/1/patch", headers={"Origin": "https://evil.test"})
    assert cross.status_code == 403
    same = client.post("/issues/1/patch", headers=ORIGIN)
    assert same.status_code == 404  # got past the origin check; the issue doesn't exist


# --- Per-repository authorisation ----------------------------------------------------------------


@pytest.fixture
def world(db_session):
    """Three repositories: the user can write to A, read B, and has no access to C."""
    owner = User(github_id=900, login="org")
    repos = {}
    for key, gid in (("A", 9001), ("B", 9002), ("C", 9003)):
        r = Repository(
            owner=owner,
            github_id=gid,
            full_name=f"org/{key}",
            installation_id=1,
            default_branch="main",
        )
        r.issue = Issue(repository=r, number=1, title=f"bug in {key}", body="")
        db_session.add_all([r, r.issue])
        repos[key] = r
    db_session.flush()
    for r in repos.values():
        r.execution = TestExecution(
            repository_id=r.id, kind="baseline", status="completed", exit_code=0, selectors=[]
        )
        db_session.add(r.execution)
        db_session.flush()
        r.patch = Patch(
            issue_id=r.issue.id,
            repository_id=r.id,
            reproduction_id=r.execution.id,
            status="verified",
            diff="d",
            edits=[],
            attempts=[],
        )
        db_session.add(r.patch)
    db_session.flush()
    return repos


@pytest.fixture
def alice(client, db_session, world):
    access = {world["A"].github_id: "write", world["B"].github_id: "read"}
    token = sessions.create_session(db_session, make_user(db_session), access)
    client.cookies.set("rp_session", token)
    return client


def test_lists_only_show_what_the_user_can_reach(alice):
    names = [r["full_name"] for r in alice.get("/repositories").json()]
    assert names == ["org/A", "org/B"]
    titles = {i["title"] for i in alice.get("/issues").json()}
    assert titles == {"bug in A", "bug in B"}


def test_unreachable_repositories_look_like_they_do_not_exist(alice, world):
    c, i, e, p = (world["C"].id, world["C"].issue.id, world["C"].execution.id, world["C"].patch.id)
    reads = [
        f"/executions/{e}",
        f"/patches/{p}",
        f"/issues/{i}/patches",
        f"/repositories/{c}/executions",
    ]
    for path in reads:
        assert alice.get(path).status_code == 404, path
    writes = [
        f"/issues/{i}/retrieve", f"/issues/{i}/explain", f"/issues/{i}/reproduce",
        f"/issues/{i}/patch", f"/repositories/{c}/baseline", f"/repositories/{c}/reindex",
        f"/patches/{p}/approve", f"/patches/{p}/reject", f"/patches/{p}/open-pr",
    ]  # fmt: skip
    for path in writes:
        assert alice.post(path, headers=ORIGIN).status_code == 404, path


def test_read_access_allows_looking_but_not_acting(alice, world):
    b = world["B"]
    for path in (
        f"/executions/{b.execution.id}",
        f"/patches/{b.patch.id}",
        f"/issues/{b.issue.id}/patches",
    ):
        assert alice.get(path).status_code == 200, path
    assert (
        alice.post(f"/issues/{b.issue.id}/retrieve", headers=ORIGIN).status_code == 409
    )  # allowed
    for path in [
        f"/issues/{b.issue.id}/reproduce", f"/issues/{b.issue.id}/patch",
        f"/repositories/{b.id}/baseline", f"/repositories/{b.id}/reindex",
        f"/patches/{b.patch.id}/approve", f"/patches/{b.patch.id}/reject",
        f"/patches/{b.patch.id}/open-pr",
    ]:  # fmt: skip
        assert alice.post(path, headers=ORIGIN).status_code == 403, path


def test_write_access_gets_past_the_check(alice, world):
    a = world["A"]
    # 422: reached the endpoint's own logic (no failing test is named), so authorisation passed.
    assert alice.post(f"/issues/{a.issue.id}/reproduce", headers=ORIGIN).status_code == 422


def test_reviewer_is_the_signed_in_user_not_whatever_the_request_says(alice, world, db_session):
    patch = world["A"].patch
    response = alice.post(
        f"/patches/{patch.id}/reject",
        headers=ORIGIN,
        json={"reviewer": "someone-else", "note": "no"},
    )
    assert response.status_code == 200
    assert db_session.get(Patch, patch.id).reviewer == "alice"


def test_admin_token_sees_everything(client, monkeypatch, world):
    monkeypatch.setattr(settings, "api_token", "shared-token-123")
    headers = {"Authorization": "Bearer shared-token-123"}
    assert [r["full_name"] for r in client.get("/repositories", headers=headers).json()] == [
        "org/A", "org/B", "org/C",
    ]  # fmt: skip
    assert client.get(f"/patches/{world['C'].patch.id}", headers=headers).status_code == 200
