import subprocess
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import IndexedFile, Repository, User
from app.worker import jobs


def git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def repo_row(db_session):
    owner = User(github_id=555_001, login="w")
    repo = Repository(owner=owner, github_id=555_002, full_name="w/demo", installation_id=1)
    db_session.add(repo)
    db_session.flush()
    return repo


@pytest.fixture
def job_env(monkeypatch, db_session, tmp_path):
    monkeypatch.setattr(jobs, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "workspaces")


def test_job_indexes_repository(job_env, repo_row, tiny_repo, db_session, monkeypatch):
    monkeypatch.setattr(jobs, "clone_source", lambda repo: (str(tiny_repo), None))
    jobs.index_repository_job(repo_row.id)

    assert repo_row.index_status == "ready"
    assert repo_row.index_error is None
    assert len(repo_row.indexed_commit) == 40
    paths = db_session.scalars(
        select(IndexedFile.path).where(IndexedFile.repository_id == repo_row.id)
    ).all()
    assert paths == ["app.py"]
    assert (settings.workspace_dir / str(repo_row.id) / "app.py").exists()

    # Running again over an existing checkout works (replaces the workspace).
    jobs.index_repository_job(repo_row.id)
    assert repo_row.index_status == "ready"


def test_job_records_failure(job_env, repo_row, tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "clone_source", lambda repo: (str(tmp_path / "missing"), None))
    jobs.index_repository_job(repo_row.id)

    assert repo_row.index_status == "failed"
    assert "git clone failed" in repo_row.index_error


def test_job_ignores_unknown_repository(job_env):
    jobs.index_repository_job(999_999_999)


def test_repositories_api(db_session, repo_row, monkeypatch):
    client = TestClient(app)
    listed = next(r for r in client.get("/repositories").json() if r["id"] == repo_row.id)
    assert listed["full_name"] == "w/demo"
    assert listed["index_status"] == "pending"
    assert listed["file_count"] == 0

    queued: list[int] = []
    monkeypatch.setattr("app.api.repositories.enqueue_index", queued.append)
    response = client.post(f"/repositories/{repo_row.id}/reindex")
    assert response.status_code == 202
    assert response.json()["index_status"] == "queued"
    assert queued == [repo_row.id]

    assert client.post(f"/repositories/{repo_row.id}/reindex").status_code == 409
    assert client.post("/repositories/999999999/reindex").status_code == 404
