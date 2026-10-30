import asyncio
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.security.limits import BodyLimitMiddleware

# --- The middleware on its own -------------------------------------------------------------------


async def run(app_under_test, chunks: list[bytes], *, headers=None, path="/x"):
    """Drive the middleware by hand; returns (status or None, body, how many chunks were pulled)."""
    sent: list[dict] = []
    pulled = 0
    queue = [*chunks]

    async def receive():
        nonlocal pulled
        if not queue:
            return {"type": "http.disconnect"}
        pulled += 1
        data = queue.pop(0)
        return {"type": "http.request", "body": data, "more_body": bool(queue)}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "headers": headers or []}
    await BodyLimitMiddleware(app_under_test)(scope, receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return (start["status"] if start else None), body, pulled


async def read_all_then_ok(scope, receive, send):
    while (await receive()).get("more_body"):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def never_reads(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"fine"})


def test_chunked_body_over_the_cap_is_refused_and_reading_stops(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 100)
    status, body, pulled = asyncio.run(run(read_all_then_ok, [b"x" * 40] * 1000))
    assert status == 413 and b"too large" in body
    assert pulled == 3  # 40, 80, 120 > 100: it did not drain the other 997 chunks


def test_a_body_exactly_at_the_cap_is_allowed_and_one_byte_more_is_not(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 100)
    assert asyncio.run(run(read_all_then_ok, [b"x" * 50, b"x" * 50]))[0] == 200
    assert asyncio.run(run(read_all_then_ok, [b"x" * 50, b"x" * 51]))[0] == 413


def test_the_application_cannot_turn_a_refusal_into_a_success(monkeypatch):
    # An app that swallows the disconnect and answers 200 anyway must still be overridden.
    async def stubborn(scope, receive, send):
        while (await receive())["type"] == "http.request":
            pass
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"processed"})

    monkeypatch.setattr(settings, "max_request_bytes", 10)
    status, body, _ = asyncio.run(run(stubborn, [b"x" * 100]))
    assert status == 413 and b"processed" not in body


def test_declared_oversize_or_garbage_length_is_refused_without_running_the_app(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 100)
    for value in (b"101", b"not-a-number", b"-5"):
        status, _, pulled = asyncio.run(
            run(never_reads, [b"x"], headers=[(b"content-length", value)])
        )
        assert status == 413 and pulled == 0, value
    assert asyncio.run(run(never_reads, [b"x"], headers=[(b"content-length", b"100")]))[0] == 200


def test_webhooks_get_their_own_larger_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 100)
    monkeypatch.setattr(settings, "max_webhook_bytes", 1000)
    chunks = [b"x" * 500]
    assert asyncio.run(run(read_all_then_ok, chunks, path="/github/webhook"))[0] == 200
    assert asyncio.run(run(read_all_then_ok, chunks, path="/issues/1/explain"))[0] == 413
    assert asyncio.run(run(read_all_then_ok, [b"x" * 600] * 2, path="/github/webhook"))[0] == 413


def test_non_http_traffic_passes_straight_through():
    seen = []

    async def lifespan_app(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(BodyLimitMiddleware(lifespan_app)({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


# --- Through the real application ----------------------------------------------------------------


def chunked(total: int, size: int = 1000):
    sent = 0
    while sent < total:
        yield b"x" * min(size, total - sent)
        sent += size


def test_chunked_uploads_cannot_bypass_the_api_cap(monkeypatch):
    monkeypatch.setattr(settings, "max_request_bytes", 5000)
    client = TestClient(app)
    headers = {"Content-Type": "application/json"}  # /reproduce parses a JSON body
    big = client.post("/issues/1/reproduce", content=chunked(50_000), headers=headers)
    assert big.status_code == 413
    small = client.post(
        "/issues/1/reproduce", content=iter([b'{"selectors"', b": []}"]), headers=headers
    )
    assert small.status_code == 404  # reached the endpoint (the issue does not exist)


def test_chunked_uploads_cannot_bypass_the_webhook_cap(monkeypatch):
    secret = "hook-secret-value"
    monkeypatch.setattr(settings, "github_webhook_secret", secret)
    monkeypatch.setattr(settings, "max_webhook_bytes", 5000)
    client = TestClient(app)
    assert client.post("/github/webhook", content=chunked(50_000)).status_code == 413

    body = json.dumps({"action": "opened"}).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": "ping", "X-Hub-Signature-256": signature}
    ok = client.post("/github/webhook", content=iter([body[:5], body[5:]]), headers=headers)
    assert ok.status_code == 202
