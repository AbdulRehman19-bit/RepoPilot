import json
import threading
from html import unescape
from pathlib import Path

import httpx
import pytest

from app.github import register
from app.github.register import Registration, build_manifest, make_server, update_env_file

SECRETS = {
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYBYTES\n-----END RSA PRIVATE KEY-----\n",
    "client_secret": "client-secret-xyz",
    "webhook_secret": "webhook-secret-abc",
}
APP = {
    "id": 98765,
    "slug": "repopilot-pilot-1a2b3c",
    "name": "repopilot-pilot-1a2b3c",
    "client_id": "Iv1.clientid",
    "html_url": "https://github.com/apps/repopilot-pilot-1a2b3c",
    **SECRETS,
}


@pytest.fixture
def setup(tmp_path):
    seen: list[str] = []

    def convert(code: str) -> dict:
        seen.append(code)
        return APP

    env = tmp_path / ".env"
    env.write_text("# my settings\nREPOPILOT_LLM_PROVIDER=ollama\nREPOPILOT_GITHUB_APP_ID=old\n")
    registration = Registration(
        manifest=build_manifest(
            "repopilot-pilot-1a2b3c", "http://localhost:5173", "http://localhost:8765/callback"
        ),
        org=None,
        env_file=env,
        key_file=tmp_path / "github-app.pem",
        frontend_url="http://localhost:5173",
        convert=convert,
    )
    server = make_server(registration, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield registration, base, seen, tmp_path
    server.shutdown()
    server.server_close()


# --- The manifest --------------------------------------------------------------------------------


def test_manifest_asks_for_exactly_the_permissions_the_pipeline_needs():
    manifest = build_manifest("n", "http://localhost:5173/", "http://localhost:8765/callback")
    assert manifest["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "read",
        "metadata": "read",
    }
    assert manifest["default_events"] == ["issues"]
    assert manifest["hook_attributes"]["active"] is False
    assert manifest["public"] is False
    assert manifest["callback_urls"] == ["http://localhost:5173/api/auth/callback"]
    assert manifest["redirect_url"] == "http://localhost:8765/callback"


# --- The local page ------------------------------------------------------------------------------


def test_start_page_posts_the_manifest_and_a_state_to_github(setup):
    registration, base, _, _ = setup
    page = httpx.get(base + "/").text
    assert f"action='https://github.com/settings/apps/new?state={registration.state}'" in page
    value = page.split("name=manifest value='")[1].split("'>")[0]
    assert json.loads(unescape(value))["name"] == "repopilot-pilot-1a2b3c"


def test_an_organisation_registration_posts_to_the_organisation_page(setup):
    registration, _, _, _ = setup
    registration.org = "my-org"
    assert registration.github_form_url.startswith(
        "https://github.com/organizations/my-org/settings/apps/new?state="
    )


def test_only_the_expected_routes_exist(setup):
    _, base, _, _ = setup
    assert httpx.get(base + "/anything").status_code == 404
    assert httpx.post(base + "/callback").status_code in (404, 501)


# --- The callback --------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "?code=abc", "?state=WRONG&code=abc", "?code=abc&state="])
def test_callback_rejects_a_missing_or_wrong_state_without_touching_anything(setup, query):
    registration, base, seen, tmp = setup
    assert httpx.get(base + "/callback" + query).status_code == 400
    assert seen == [] and not (tmp / "github-app.pem").exists()
    assert "old" in (tmp / ".env").read_text() and not registration.done.is_set()


def test_a_valid_callback_saves_credentials_and_never_shows_them(setup):
    registration, base, seen, tmp = setup
    response = httpx.get(f"{base}/callback?code=one-time-code&state={registration.state}")
    assert response.status_code == 200 and seen == ["one-time-code"]
    for secret in (*SECRETS.values(), "SECRETKEYBYTES"):
        assert secret not in response.text
    assert "installations/new" in response.text

    assert (tmp / "github-app.pem").read_text() == SECRETS["pem"]
    env = (tmp / ".env").read_text()
    assert "# my settings" in env and "REPOPILOT_LLM_PROVIDER=ollama" in env  # untouched
    assert "REPOPILOT_GITHUB_APP_ID=98765" in env and "=old" not in env  # replaced, not duplicated
    assert env.count("REPOPILOT_GITHUB_APP_ID=") == 1
    assert "REPOPILOT_GITHUB_CLIENT_ID=Iv1.clientid" in env
    assert "REPOPILOT_GITHUB_CLIENT_SECRET=client-secret-xyz" in env
    assert "REPOPILOT_GITHUB_WEBHOOK_SECRET=webhook-secret-abc" in env
    assert f"REPOPILOT_GITHUB_PRIVATE_KEY_PATH={tmp / 'github-app.pem'}" in env
    assert registration.done.is_set() and registration.result["slug"] == "repopilot-pilot-1a2b3c"


def test_the_code_works_once(setup):
    registration, base, seen, _ = setup
    url = f"{base}/callback?code=c&state={registration.state}"
    assert httpx.get(url).status_code == 200
    assert httpx.get(url).status_code == 400
    assert seen == ["c"]


def test_a_webhook_secret_is_generated_when_github_sends_none(setup):
    registration, base, _, tmp = setup
    registration.convert = lambda code: {**APP, "webhook_secret": None}
    httpx.get(f"{base}/callback?code=c&state={registration.state}")
    line = next(x for x in (tmp / ".env").read_text().splitlines() if "WEBHOOK_SECRET" in x)
    assert len(line.split("=", 1)[1]) >= 32


def test_a_rejected_code_writes_nothing_and_can_be_retried(setup):
    registration, base, _, tmp = setup
    good = registration.convert

    def rejected(code):
        raise httpx.HTTPStatusError(
            "404", request=httpx.Request("POST", "x"), response=httpx.Response(404)
        )

    registration.convert = rejected
    assert httpx.get(f"{base}/callback?code=c&state={registration.state}").status_code == 502
    assert not (tmp / "github-app.pem").exists() and not registration.done.is_set()

    registration.convert = good
    assert httpx.get(f"{base}/callback?code=c2&state={registration.state}").status_code == 200


def test_the_server_only_listens_on_loopback(setup):
    _, base, _, _ = setup
    assert base.startswith("http://127.0.0.1:")


def test_the_log_never_echoes_the_one_time_code(setup, capsys):
    registration, base, _, _ = setup
    httpx.get(f"{base}/callback?code=SUPERSECRETCODE&state={registration.state}")
    captured = capsys.readouterr()
    assert "SUPERSECRETCODE" not in captured.out + captured.err


# --- Helpers -------------------------------------------------------------------------------------


def test_update_env_file_creates_replaces_and_preserves(tmp_path):
    path = tmp_path / ".env"
    update_env_file(path, {"A": "1", "B": "2"})
    assert path.read_text() == "A=1\nB=2\n"
    path.write_text("# keep\nA=old\n\nC=3\n")
    update_env_file(path, {"A": "new", "D": "4"})
    assert path.read_text() == "# keep\nA=new\n\nC=3\nD=4\n"


def test_update_env_file_does_not_treat_comments_as_settings(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# A=commented\n")
    update_env_file(path, {"A": "real"})
    assert path.read_text() == "# A=commented\nA=real\n"


def test_main_refuses_to_overwrite_an_existing_key_file(tmp_path, capsys):
    key = tmp_path / "github-app.pem"
    key.write_text("existing")
    code = register.main(
        ["--key-file", str(key), "--env-file", str(tmp_path / ".env"), "--no-browser"]
    )
    assert code == 2 and key.read_text() == "existing"
    assert "already exists" in capsys.readouterr().err


def test_default_locations_are_inside_the_backend_folder():
    assert register.BACKEND_DIR == Path(__file__).resolve().parents[1]
