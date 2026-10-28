import hashlib
import hmac
import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.config import settings
from app.github.auth import create_app_jwt, mint_installation_token
from app.github.webhooks import verify_signature
from app.main import app

SECRET = "test-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture(scope="module")
def rsa_pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


def test_verify_signature():
    body = b'{"action":"opened"}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(body, "other"))
    assert not verify_signature(SECRET, body, None)
    assert not verify_signature(SECRET, body, "sha1=abc")
    assert not verify_signature("", body, sign(body, ""))


def test_webhook_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "github_webhook_secret", SECRET)
    client = TestClient(app)
    body = json.dumps({"action": "opened"}).encode()
    headers = {"X-GitHub-Event": "ping", "X-Hub-Signature-256": sign(body)}

    ok = client.post("/github/webhook", content=body, headers=headers)
    assert ok.status_code == 202
    assert ok.json() == {"event": "ping", "action": "opened", "handled": False}

    bad = client.post(
        "/github/webhook", content=body, headers={**headers, "X-Hub-Signature-256": "sha256=0"}
    )
    assert bad.status_code == 401


def test_app_jwt(rsa_pem):
    private, public = rsa_pem
    token = create_app_jwt("123", private, now=1_000_000)
    claims = jwt.decode(token, public, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123"
    assert claims["exp"] - claims["iat"] <= 10 * 60


def test_mint_installation_token(rsa_pem, monkeypatch, tmp_path):
    private, public = rsa_pem
    key_file = tmp_path / "app.pem"
    key_file.write_text(private)
    monkeypatch.setattr(settings, "github_app_id", "123")
    monkeypatch.setattr(settings, "github_private_key_path", key_file)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/app/installations/42/access_tokens"
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        assert jwt.decode(bearer, public, algorithms=["RS256"])["iss"] == "123"
        return httpx.Response(201, json={"token": "ghs_abc", "expires_at": "2030-01-01T00:00:00Z"})

    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handler))
    result = mint_installation_token(42, client=client)
    assert result.token == "ghs_abc"
