import uuid

import pytest
from fastapi.testclient import TestClient
from redis import Redis

from app.api import ratelimit
from app.auth import sessions
from app.config import settings
from app.main import app
from app.models import User

REAL_HIT = ratelimit.hit  # the autouse fixture replaces ratelimit.hit for other tests
ORIGIN = {"Origin": "http://testserver"}


# --- The counter, against the real Redis ---------------------------------------------------------


def test_counter_allows_up_to_the_limit_then_reports_when_it_resets():
    key = f"test-{uuid.uuid4().hex}"
    assert [REAL_HIT(key, 3, 60) for _ in range(3)] == [None, None, None]
    wait = REAL_HIT(key, 3, 60)
    assert wait is not None and 1 <= wait <= 60
    assert REAL_HIT(f"other-{key}", 3, 60) is None  # keys are independent
    Redis.from_url(settings.redis_url).delete(f"{settings.queue_name}:rate:{key}")


def test_counter_always_gets_an_expiry():
    key = f"test-{uuid.uuid4().hex}"
    REAL_HIT(key, 5, 120)
    redis = Redis.from_url(settings.redis_url)
    ttl = redis.ttl(f"{settings.queue_name}:rate:{key}")
    assert 0 < ttl <= 120
    redis.delete(f"{settings.queue_name}:rate:{key}")


def test_limiter_fails_open_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    assert REAL_HIT("anything", 1, 60) is None
    assert REAL_HIT("anything", 1, 60) is None


# --- On the routes -------------------------------------------------------------------------------


@pytest.fixture
def counted(monkeypatch):
    """An in-memory counter standing in for Redis, so route behaviour is tested deterministically."""
    seen: dict[str, int] = {}

    def fake(key, limit, window):
        seen[key] = seen.get(key, 0) + 1
        return None if seen[key] <= limit else 42

    monkeypatch.setattr(ratelimit, "hit", fake)
    return seen


@pytest.fixture
def client(db_session):
    return TestClient(app, follow_redirects=False)


def test_expensive_routes_return_429_with_retry_after(client, counted, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_jobs_per_hour", 2)
    for _ in range(2):
        assert client.post("/issues/1/reproduce").status_code == 404  # allowed, issue missing
    blocked = client.post("/issues/1/reproduce")
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == "42"
    assert "rate limit" in blocked.json()["detail"]


def test_all_job_routes_share_one_budget_and_explain_has_its_own(client, counted, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_jobs_per_hour", 3)
    monkeypatch.setattr(settings, "rate_limit_ai_per_hour", 1)
    assert client.post("/issues/1/reproduce").status_code == 404
    assert client.post("/issues/1/patch").status_code == 404
    assert client.post("/repositories/1/baseline").status_code == 404
    assert client.post("/repositories/1/reindex").status_code == 429  # fourth job
    assert client.post("/issues/1/explain").status_code == 404  # separate model budget
    assert client.post("/issues/1/explain").status_code == 429


def test_each_user_has_their_own_budget(client, counted, monkeypatch, db_session):
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    monkeypatch.setattr(settings, "session_cookie_secure", False)
    monkeypatch.setattr(settings, "rate_limit_jobs_per_hour", 1)
    tokens = {}
    for gid, login in ((1, "alice"), (2, "bob")):
        user = User(github_id=gid, login=login)
        db_session.add(user)
        db_session.flush()
        tokens[login] = sessions.create_session(db_session, user, {})

    def post(login):
        client.cookies.set("rp_session", tokens[login])
        return client.post("/issues/1/reproduce", headers=ORIGIN).status_code

    assert post("alice") == 404
    assert post("alice") == 429
    assert post("bob") == 404  # alice using hers up does not touch bob's
    assert "user:alice" in "".join(counted) and "user:bob" in "".join(counted)


def test_a_limit_of_zero_disables_it(client, counted, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_jobs_per_hour", 0)
    for _ in range(5):
        assert client.post("/issues/1/reproduce").status_code == 404
    assert counted == {}


def test_sign_in_endpoints_are_limited_per_address(client, counted, monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    monkeypatch.setattr(settings, "rate_limit_login_per_minute", 2)
    assert client.get("/auth/login").status_code == 302
    assert client.get("/auth/callback?code=x&state=y").status_code == 400
    assert client.get("/auth/login").status_code == 429  # login and callback share the budget
    assert any(key.startswith("login:ip:") for key in counted)


def test_cheap_routes_are_not_limited(client, counted, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_jobs_per_hour", 1)
    for _ in range(5):
        assert client.get("/repositories").status_code == 200
        assert client.get("/health").status_code == 200
    assert counted == {}
