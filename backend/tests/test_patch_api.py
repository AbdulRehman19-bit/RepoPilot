import json
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.llm.fake import FakeProvider
from app.main import app
from app.models import IndexedFile, Issue, Patch, Repository, TestExecution, User
from app.worker import patch_jobs
from tests.helpers import requires_docker

BUGGY = "def add(a, b):\n    return a - b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
TARGET = "test_calc.py::test_add"


@pytest.fixture
def issue_row(db_session):
    repo = Repository(
        owner=User(github_id=111_001, login="p"),
        github_id=111_002,
        full_name="p/demo",
        index_status="ready",
        indexed_commit="b" * 40,
    )
    repo.files.append(
        IndexedFile(path="calc.py", language="python", size=len(BUGGY), line_count=2, content=BUGGY)
    )
    repo.files.append(
        IndexedFile(
            path="test_calc.py", language="python", size=len(TEST), line_count=5, content=TEST
        )
    )
    issue = Issue(
        repository=repo,
        number=1,
        title="add in calc.py returns the wrong result",
        body=f"`{TARGET}` fails",
    )
    db_session.add_all([repo, issue])
    db_session.flush()
    return issue


def reproduction(db_session, issue, **overrides) -> TestExecution:
    values = {
        "repository_id": issue.repository_id,
        "issue_id": issue.id,
        "kind": "reproduction",
        "status": "completed",
        "exit_code": 1,
        "failed": 1,
        "selectors": [TARGET],
        "failures": [{"kind": "FAILED", "node_id": TARGET, "message": "assert -1 == 3"}],
        "output": "E   assert -1 == 3",
    }
    execution = TestExecution(**{**values, **overrides})
    db_session.add(execution)
    db_session.flush()
    return execution


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    ids: list[int] = []
    monkeypatch.setattr("app.api.patches.enqueue_patch", ids.append)
    monkeypatch.setattr("app.api.patches.get_provider", lambda: FakeProvider())
    return ids


def test_start_patch_uses_the_latest_reproduction(db_session, issue_row, queued):
    reproduction(db_session, issue_row, exit_code=0, failed=0)  # older, not reproduced
    good = reproduction(db_session, issue_row)

    response = TestClient(app).post(f"/issues/{issue_row.id}/patch")

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["reproduction_id"]) == ("queued", good.id)
    assert queued == [body["id"]]
    assert TestClient(app).get(f"/patches/{body['id']}").json()["id"] == body["id"]
    assert [p["id"] for p in TestClient(app).get(f"/issues/{issue_row.id}/patches").json()] == [
        body["id"]
    ]


def test_patch_requires_a_reproduced_failure(db_session, issue_row, queued):
    client = TestClient(app)
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409  # nothing run yet

    reproduction(db_session, issue_row, exit_code=0, failed=0)  # tests pass: not reproduced
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409
    reproduction(db_session, issue_row, exit_code=5)  # nothing collected
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409
    reproduction(db_session, issue_row, exit_code=124, timed_out=True)
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409
    reproduction(db_session, issue_row, status="running", exit_code=None)
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409
    assert queued == []


def test_patch_needs_a_model_and_an_indexed_repo(db_session, issue_row, queued, monkeypatch):
    reproduction(db_session, issue_row)
    client = TestClient(app)

    monkeypatch.setattr("app.api.patches.get_provider", lambda: None)
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409

    monkeypatch.setattr("app.api.patches.get_provider", lambda: FakeProvider())
    issue_row.repository.index_status = "pending"
    db_session.flush()
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409
    assert queued == []


def test_one_patch_at_a_time_and_unknown_ids(db_session, issue_row, queued):
    reproduction(db_session, issue_row)
    client = TestClient(app)
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 202
    assert client.post(f"/issues/{issue_row.id}/patch").status_code == 409
    assert client.post("/issues/999999999/patch").status_code == 404
    assert client.get("/patches/999999999").status_code == 404


def test_reproduction_from_another_repository_is_rejected(db_session, issue_row, queued):
    other = Repository(owner=issue_row.repository.owner, github_id=111_003, full_name="p/other")
    db_session.add(other)
    db_session.flush()
    foreign = reproduction(db_session, issue_row, repository_id=other.id)

    response = TestClient(app).post(
        f"/issues/{issue_row.id}/patch", json={"reproduction_id": foreign.id}
    )
    assert response.status_code == 404
    assert queued == []


# --- The real job against real containers; only the model is scripted ------------------------


def edit_reply(replace: str, path: str = "calc.py") -> str:
    return json.dumps(
        {
            "explanation": "add subtracted",
            "edits": [{"path": path, "search": "    return a - b", "replace": replace}],
        }
    )


@pytest.fixture
def job_env(db_session, issue_row, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    checkout = settings.workspace_dir / str(issue_row.repository_id)
    checkout.mkdir(parents=True)
    (checkout / "calc.py").write_text(BUGGY)
    (checkout / "test_calc.py").write_text(TEST)
    monkeypatch.setattr(patch_jobs, "SessionLocal", lambda: nullcontext(db_session))

    repro = reproduction(db_session, issue_row)
    patch = Patch(
        issue_id=issue_row.id,
        repository_id=issue_row.repository_id,
        reproduction_id=repro.id,
        status="queued",
    )
    db_session.add(patch)
    db_session.flush()
    return patch, checkout


@requires_docker
def test_job_produces_a_verified_patch_end_to_end(job_env, monkeypatch):
    patch, checkout = job_env
    provider = FakeProvider([edit_reply("    return a + b")], model="scripted-model")
    monkeypatch.setattr(patch_jobs, "get_provider", lambda: provider)

    patch_jobs.run_patch_job(patch.id)

    assert patch.status == "verified", patch.error or patch.attempts
    assert patch.model_name == "scripted-model"
    assert "+    return a + b" in patch.diff
    assert [a["outcome"] for a in patch.attempts] == ["verified"]
    assert patch.attempts[0]["full"]["passed"] >= 1
    assert patch.commit_sha == "b" * 40
    assert (checkout / "calc.py").read_text() == BUGGY  # the real checkout is never modified
    assert "calc.py" in provider.prompts[0] and "assert -1 == 3" in provider.prompts[0]


@requires_docker
def test_job_reports_failure_when_no_attempt_works(job_env, monkeypatch):
    patch, _ = job_env
    provider = FakeProvider(lambda prompt: edit_reply("    return a * b"))
    monkeypatch.setattr(patch_jobs, "get_provider", lambda: provider)

    patch_jobs.run_patch_job(patch.id)

    assert patch.status == "failed"
    assert patch.diff is None
    assert [a["outcome"] for a in patch.attempts] == ["target_failed"] * settings.patch_max_attempts
    assert len(provider.prompts) == settings.patch_max_attempts
    assert "assert" in provider.prompts[1]  # real pytest output was fed back


def test_job_records_a_disabled_model(job_env, monkeypatch):
    patch, _ = job_env
    monkeypatch.setattr(patch_jobs, "get_provider", lambda: None)
    patch_jobs.run_patch_job(patch.id)
    assert patch.status == "error" and "disabled" in patch.error


def test_job_records_an_unavailable_model(job_env, monkeypatch):
    patch, _ = job_env
    down = FakeProvider(available=(False, "model 'x' is not installed; run: ollama pull x"))
    monkeypatch.setattr(patch_jobs, "get_provider", lambda: down)
    patch_jobs.run_patch_job(patch.id)
    assert patch.status == "error" and "ollama pull" in patch.error
