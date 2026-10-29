import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.github import replay, router
from app.main import app
from app.security.redact import REDACTED, redact

SECRET = "hook-secret-value"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


# --- Redaction -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "ghs_" + "a1B2c3D4e5F6g7H8i9J0k1L2",
        "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4",
        "github_pat_" + "11ABCDEFG0abcdefghij_klmnopqrstuvwxyz",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxMjMifQ.c2lnbmF0dXJlMTIz",
    ],
)
def test_known_token_shapes_are_redacted(secret):
    text = f"error while calling the API with {secret} as the key"
    assert secret not in redact(text)
    assert REDACTED in redact(text)


def test_private_keys_are_redacted_even_when_truncated():
    body = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nabcdef"  # output cap cut the END line
    )
    out = redact(f"before\n{body}")
    assert "MIIEow" not in out and out.startswith("before")
    full = "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\nafter"
    assert redact(full) == f"{REDACTED}\nafter"


def test_authorization_headers_and_url_credentials_are_redacted():
    assert "hunter22" not in redact("Authorization: Bearer hunter22")
    assert "hunter22" not in redact("{'authorization': 'token hunter22'}")
    cleaned = redact("fatal: unable to access https://x-access-token:hunter22@github.com/o/r.git/")
    assert "hunter22" not in cleaned
    assert "x-access-token:" in cleaned and "@github.com" in cleaned


def test_configured_secrets_are_redacted_but_short_ones_are_not(monkeypatch):
    monkeypatch.setattr(settings, "api_token", "correct-horse-battery")
    monkeypatch.setattr(settings, "github_webhook_secret", "abc")  # too short to be safe to replace
    out = redact("token=correct-horse-battery and abc stay")
    assert "correct-horse-battery" not in out
    assert "abc stay" in out


def test_ordinary_text_is_untouched():
    text = "E   assert 3 == 4\ntests/test_x.py:12: AssertionError\nhttps://example.com/path"
    assert redact(text) == text


# --- Webhook hardening ---------------------------------------------------------------------------


@pytest.fixture
def hook(monkeypatch):
    """A client for the webhook with an in-memory stand-in for the replay store."""
    seen: set[bytes] = set()
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    store = SimpleNamespace(
        claim=lambda body: not (body in seen or seen.add(body)),
        release=lambda body: seen.discard(body),
    )
    monkeypatch.setattr(router, "replay", store)
    client = TestClient(app)

    def post(body: bytes, event="ping", signature=None):
        headers = {"X-GitHub-Event": event, "X-Hub-Signature-256": signature or sign(body)}
        return client.post("/github/webhook", content=body, headers=headers)

    return post


def test_oversized_webhook_is_rejected_before_signature_check(hook, monkeypatch):
    monkeypatch.setattr(settings, "max_webhook_bytes", 100)
    big = json.dumps({"action": "opened", "pad": "x" * 500}).encode()
    assert hook(big).status_code == 413
    assert hook(big, signature="sha256=bad").status_code == 413  # not even checked


def test_signed_but_malformed_payloads_are_client_errors(hook):
    assert hook(b"{not json").status_code == 400
    assert hook(b'["a list"]').status_code == 400
    assert hook(b"[1]", event="issues").status_code == 400


def test_signed_payload_with_unexpected_shape_is_422_not_500(hook, db_session):
    body = json.dumps({"action": "opened", "issue": {}}).encode()  # no repository key
    response = hook(body, event="issues")
    assert response.status_code == 422
    # The failed delivery is forgotten so GitHub's redelivery is processed, not swallowed.
    assert hook(body, event="issues").status_code == 422


def test_replayed_webhook_is_not_processed_twice(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(router, "_process", lambda s, e, p: calls.append(e) or True)
    body = json.dumps({"action": "edited"}).encode()
    assert hook(body, event="issues").json()["handled"] is True
    replayed = hook(body, event="issues")
    assert replayed.status_code == 202 and replayed.json()["handled"] is False
    assert calls == ["issues"]


def test_replay_key_ignores_unsigned_headers():
    # The delivery id header is not covered by GitHub's signature, so the key is the body alone.
    assert replay._key(b"same") == replay._key(b"same")
    assert replay._key(b"same") != replay._key(b"other")


def test_replay_protection_fails_open_when_redis_is_down(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    assert replay.claim(b"anything") is True
    replay.release(b"anything")  # must not raise


# --- API request size ----------------------------------------------------------------------------


def test_api_rejects_oversized_bodies_but_not_the_webhook_path(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 50)
    client = TestClient(app)
    big = b"x" * 200
    assert client.post("/issues/1/explain", content=big).status_code == 413
    # The webhook has its own (larger) limit and still reaches its signature check.
    assert client.post("/github/webhook", content=big).status_code == 401
    assert client.get("/health").status_code == 200
