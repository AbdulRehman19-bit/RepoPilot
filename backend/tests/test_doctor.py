import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import doctor
from app.config import settings
from app.doctor import FAIL, PASS, WARN, Result


@pytest.fixture(scope="module")
def pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def statuses(results):
    return {r.name: r.status for r in results}


def test_github_app_checks_pass_with_a_real_key_and_secret(monkeypatch, tmp_path, pem):
    key = tmp_path / "app.pem"
    key.write_bytes(pem)
    monkeypatch.setattr(settings, "github_app_id", "123")
    monkeypatch.setattr(settings, "github_private_key_path", key)
    monkeypatch.setattr(settings, "github_webhook_secret", "s3cret")
    assert set(statuses(doctor.check_github_app()).values()) == {PASS}


def test_github_app_checks_name_each_missing_piece(monkeypatch):
    monkeypatch.setattr(settings, "github_app_id", "")
    monkeypatch.setattr(settings, "github_private_key_path", None)
    monkeypatch.setattr(settings, "github_webhook_secret", "")
    results = doctor.check_github_app()
    assert statuses(results) == {
        "github app": FAIL,
        "github key": FAIL,
        "webhook secret": FAIL,
    }
    assert all("REPOPILOT_" in r.detail for r in results)


def test_a_file_that_is_not_a_private_key_is_reported(monkeypatch, tmp_path):
    bad = tmp_path / "app.pem"
    bad.write_text("this is not a key")
    monkeypatch.setattr(settings, "github_app_id", "1")
    monkeypatch.setattr(settings, "github_private_key_path", bad)
    monkeypatch.setattr(settings, "github_webhook_secret", "x")
    assert statuses(doctor.check_github_app())["github key"] == FAIL


@pytest.mark.parametrize(
    ("client_id", "client_secret", "token", "expected"),
    [
        ("id", "", "", FAIL),  # half configured
        ("", "secret", "", FAIL),
        ("id", "secret", "", PASS),
        ("", "", "tok", WARN),  # a shared token only
        ("", "", "", WARN),  # open development mode
    ],
)
def test_signin_configuration(monkeypatch, client_id, client_secret, token, expected):
    monkeypatch.setattr(settings, "github_client_id", client_id)
    monkeypatch.setattr(settings, "github_client_secret", client_secret)
    monkeypatch.setattr(settings, "api_token", token)
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:5173")
    assert statuses(doctor.check_signin())["sign-in"] == expected


def test_secure_cookies_over_plain_http_on_a_real_host_are_flagged(monkeypatch):
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    monkeypatch.setattr(settings, "session_cookie_secure", True)
    monkeypatch.setattr(settings, "frontend_url", "http://repopilot.example.com")
    assert statuses(doctor.check_signin())["cookies"] == FAIL
    monkeypatch.setattr(settings, "frontend_url", "https://repopilot.example.com")
    assert "cookies" not in statuses(doctor.check_signin())
    monkeypatch.setattr(settings, "frontend_url", "http://localhost:5173")
    assert "cookies" not in statuses(doctor.check_signin())  # browsers exempt localhost


def online(monkeypatch, tmp_path, pem, answer: httpx.Response):
    key = tmp_path / "app.pem"
    key.write_bytes(pem)
    monkeypatch.setattr(settings, "github_app_id", "123")
    monkeypatch.setattr(settings, "github_private_key_path", key)
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["Authorization"][:7]))
        return answer

    client = httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(handle))
    return doctor.check_github_online(client), seen


def test_online_check_passes_for_a_correctly_configured_app(monkeypatch, tmp_path, pem):
    app = {
        "slug": "repopilot-pilot",
        "permissions": {
            "contents": "write",
            "pull_requests": "write",
            "issues": "read",
            "metadata": "read",
        },
        "events": ["issues", "push"],
    }
    results, seen = online(monkeypatch, tmp_path, pem, httpx.Response(200, json=app))
    assert set(statuses(results).values()) == {PASS}
    assert seen == [("GET", "/app", "Bearer ")]  # read-only


def test_online_check_reports_missing_permissions_and_events(monkeypatch, tmp_path, pem):
    app = {
        "slug": "x",
        "permissions": {"contents": "read", "issues": "write"},  # contents too weak, no PRs
        "events": ["push"],
    }
    results, _ = online(monkeypatch, tmp_path, pem, httpx.Response(200, json=app))
    by_name = {r.name: r for r in results}
    assert by_name["permissions"].status == FAIL
    assert "contents:write" in by_name["permissions"].detail
    assert "pull_requests:write" in by_name["permissions"].detail
    assert "issues:read" not in by_name["permissions"].detail  # write satisfies read
    assert by_name["events"].status == FAIL
    assert "issues" in by_name["events"].detail


@pytest.mark.parametrize(("status", "words"), [(401, "wrong App ID"), (500, "500")])
def test_online_check_explains_github_errors(monkeypatch, tmp_path, pem, status, words):
    results, _ = online(monkeypatch, tmp_path, pem, httpx.Response(status, json={}))
    assert len(results) == 1 and results[0].status == FAIL and words in results[0].detail


def test_run_collects_every_result_and_main_exit_status_follows_failures(capsys):
    def good():
        return Result(PASS, "a", "fine")

    def mixed():
        return [Result(WARN, "b", "hmm"), Result(FAIL, "c", "broken")]

    results = doctor.run(checks=[good, mixed])
    assert [r.name for r in results] == ["a", "b", "c"]

    original = doctor.run
    try:
        doctor.run = lambda online=False, checks=None: results
        assert doctor.main([]) == 1
        out = capsys.readouterr().out
        assert "FAIL  c" in out and "3 checks: 1 failed, 1 warnings" in out
        doctor.run = lambda online=False, checks=None: [Result(PASS, "a", "ok")]
        assert doctor.main([]) == 0
    finally:
        doctor.run = original
