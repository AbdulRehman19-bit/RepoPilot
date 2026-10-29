import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.models import Issue, Patch, Repository, TestExecution, User
from app.patching.apply import Edit, apply_to_texts
from tests.fake_github import FakeGitHub

BUGGY = "def add(a, b):\n    return a - b\n"
EDITS = [{"path": "calc.py", "search": "    return a - b", "replace": "    return a + b"}]
DIFF = apply_to_texts({"calc.py": BUGGY}, [Edit(**e) for e in EDITS]).diff
SHA = "a" * 40
TOKEN = "s3cret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def github(monkeypatch) -> FakeGitHub:
    gh = FakeGitHub({"calc.py": BUGGY.encode()})
    monkeypatch.setattr("app.api.patches.get_github_client", lambda installation_id: gh.client())
    return gh


@pytest.fixture
def client(monkeypatch, db_session) -> TestClient:
    monkeypatch.setattr(settings, "api_token", TOKEN)
    http = TestClient(app)
    http.headers.update(AUTH)
    return http


@pytest.fixture
def patch_row(db_session) -> Patch:
    repo = Repository(
        owner=User(github_id=77_001, login="o"),
        github_id=77_002,
        full_name="o/r",
        default_branch="main",
        installation_id=42,
        index_status="ready",
        indexed_commit=SHA,
    )
    issue = Issue(repository=repo, number=12, title="add is wrong", body="")
    db_session.add_all([repo, issue])
    db_session.flush()
    repro = TestExecution(
        repository_id=repo.id,
        issue_id=issue.id,
        kind="reproduction",
        status="completed",
        exit_code=1,
        selectors=["test_calc.py::test_add"],
        failures=[{"kind": "FAILED", "node_id": "test_calc.py::test_add", "message": "x"}],
        output="E   assert -1 == 3",
    )
    db_session.add(repro)
    db_session.flush()
    patch = Patch(
        issue_id=issue.id,
        repository_id=repo.id,
        reproduction_id=repro.id,
        status="verified",
        commit_sha=SHA,
        explanation="add subtracted instead of adding",
        diff=DIFF,
        edits=EDITS,
        attempts=[
            {"number": 1, "outcome": "target_failed"},
            {
                "number": 2,
                "outcome": "verified",
                "targeted": {"passed": 1, "failed": 0},
                "full": {"passed": 9, "failed": 0},
            },
        ],
    )
    db_session.add(patch)
    db_session.flush()
    return patch


# --- API token ---------------------------------------------------------------------------------


def test_api_requires_the_token_when_one_is_configured(monkeypatch, db_session):
    monkeypatch.setattr(settings, "api_token", TOKEN)
    anonymous = TestClient(app)

    assert anonymous.get("/repositories").status_code == 401
    assert (
        anonymous.get("/repositories", headers={"Authorization": "Bearer nope"}).status_code == 401
    )
    assert anonymous.get("/repositories", headers={"Authorization": TOKEN}).status_code == 200
    assert anonymous.get("/repositories", headers=AUTH).status_code == 200
    assert anonymous.post("/repositories/1/reindex").status_code == 401
    assert anonymous.get("/repositories").headers["www-authenticate"] == "Bearer"

    # Health stays public, and the webhook uses GitHub's signature instead of the token.
    assert anonymous.get("/health").status_code == 200
    assert anonymous.post("/github/webhook", content=b"{}").status_code == 401  # bad signature


def test_api_is_open_without_a_token_but_approvals_are_not(monkeypatch, db_session, patch_row):
    monkeypatch.setattr(settings, "api_token", "")
    anonymous = TestClient(app)
    assert anonymous.get("/repositories").status_code == 200
    for action in ("approve", "reject", "open-pr"):
        response = anonymous.post(f"/patches/{patch_row.id}/{action}")
        assert response.status_code == 503, action
    assert patch_row.review_status is None


# --- Approving ---------------------------------------------------------------------------------


def test_approve_opens_a_draft_pr_and_records_the_decision(client, github, patch_row):
    response = client.post(
        f"/patches/{patch_row.id}/approve", json={"reviewer": "Sam", "note": "ship it"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["review_status"] == "approved"
    assert (body["reviewer"], body["review_note"]) == ("Sam", "ship it")
    assert body["pr_url"] == "https://github.test/o/r/pull/7"
    assert body["branch_name"] == f"repopilot/issue-12-patch-{patch_row.id}"
    assert body["pr_error"] is None

    [pull] = github.pulls
    assert pull["draft"] is True and pull["base"] == "main"
    assert "Approved by Sam" in pull["body"] and "ship it" in pull["body"]
    assert "Attempts needed: 2" in pull["body"]
    assert github.branches[body["branch_name"]]["calc.py"] == b"def add(a, b):\n    return a + b\n"


def test_review_view_includes_before_and_after(client, patch_row):
    body = client.get(f"/patches/{patch_row.id}").json()
    assert body["before"]["failing"] == ["test_calc.py::test_add"]
    assert "assert -1 == 3" in body["before"]["output"]
    assert body["after"]["targeted"]["passed"] == 1
    assert body["after"]["full"]["passed"] == 9


def test_a_pull_request_is_only_opened_once(client, github, patch_row):
    assert client.post(f"/patches/{patch_row.id}/approve").status_code == 200
    again = client.post(f"/patches/{patch_row.id}/approve")
    assert again.status_code == 409 and "already opened" in again.json()["detail"]
    assert client.post(f"/patches/{patch_row.id}/open-pr").status_code == 409
    assert len(github.pulls) == 1


@pytest.mark.parametrize("status", ["queued", "running", "failed", "error"])
def test_only_verified_patches_can_be_approved(client, github, patch_row, status):
    patch_row.status = status
    response = client.post(f"/patches/{patch_row.id}/approve")
    assert response.status_code == 409
    assert patch_row.review_status is None
    assert github.requests == []


def test_stale_base_is_refused_and_the_decision_is_kept(client, github, patch_row):
    github.head_sha = "c" * 40

    response = client.post(f"/patches/{patch_row.id}/approve")

    assert response.status_code == 409
    assert "has moved" in response.json()["detail"]
    assert patch_row.review_status == "approved"  # the human decision is not lost
    assert "has moved" in patch_row.pr_error
    assert github.branches == {} and patch_row.pr_url is None


def test_github_failure_is_a_502_and_can_be_retried(client, github, patch_row):
    github.fail_everything = True
    failed = client.post(f"/patches/{patch_row.id}/approve")
    assert failed.status_code == 502
    assert patch_row.review_status == "approved" and "500" in patch_row.pr_error

    github.fail_everything = False
    retried = client.post(f"/patches/{patch_row.id}/open-pr")
    assert retried.status_code == 200
    assert retried.json()["pr_url"] and retried.json()["pr_error"] is None
    assert len(github.pulls) == 1


def test_retry_requires_prior_approval(client, github, patch_row):
    assert client.post(f"/patches/{patch_row.id}/open-pr").status_code == 409
    assert github.requests == []


def test_repository_must_be_connected_to_the_app(client, github, patch_row, db_session):
    db_session.get(Repository, patch_row.repository_id).installation_id = None
    response = client.post(f"/patches/{patch_row.id}/approve")
    assert response.status_code == 409 and "GitHub App" in response.json()["detail"]
    assert github.requests == []


def test_tampered_diff_is_caught_before_anything_is_written(client, github, patch_row):
    patch_row.diff = DIFF.replace("+    return a + b", "+    return a + b + 1")
    response = client.post(f"/patches/{patch_row.id}/approve")
    assert response.status_code == 409 and "differ" in response.json()["detail"]
    assert github.branches == {}


# --- Rejecting ---------------------------------------------------------------------------------


def test_reject_records_the_decision_and_blocks_approval(client, github, patch_row):
    response = client.post(f"/patches/{patch_row.id}/reject", json={"note": "wrong approach"})

    assert response.status_code == 200
    assert response.json()["review_status"] == "rejected"
    assert response.json()["review_note"] == "wrong approach"
    assert client.post(f"/patches/{patch_row.id}/approve").status_code == 409
    assert client.post(f"/patches/{patch_row.id}/reject").status_code == 409
    assert github.requests == []


def test_an_approved_patch_cannot_be_rejected(client, github, patch_row):
    client.post(f"/patches/{patch_row.id}/approve")
    assert client.post(f"/patches/{patch_row.id}/reject").status_code == 409


def test_unknown_patch_and_oversized_input(client, patch_row):
    assert client.post("/patches/999999999/approve").status_code == 404
    assert client.post("/patches/999999999/reject").status_code == 404
    too_long = {"note": "x" * 1001}
    assert client.post(f"/patches/{patch_row.id}/approve", json=too_long).status_code == 422
