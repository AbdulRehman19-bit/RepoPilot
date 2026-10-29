import socket
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.main import app
from app.models import Issue, Repository, TestExecution, User
from app.sandbox import deps as deps_module
from app.sandbox.deps import DependencyEnv, prepare_dependencies
from app.sandbox.pytest_runner import run_pytest
from app.worker import execution_jobs
from tests.helpers import requires_docker


def _pypi_reachable() -> bool:
    try:
        socket.create_connection(("pypi.org", 443), timeout=3).close()
    except OSError:
        return False
    return True


requires_pypi = pytest.mark.skipif(not _pypi_reachable(), reason="PyPI not reachable")

NO_DEPS = DependencyEnv(None, "none", True, [])


def write_project(root: Path, requirements: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (root / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    if requirements is not None:
        (root / "requirements.txt").write_text(requirements, encoding="utf-8")
    return root


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(settings, "workspace_dir", tmp_path / "ws")
    return tmp_path


@pytest.fixture
def repo_row(db_session):
    repo = Repository(
        owner=User(github_id=333_001, login="e"),
        github_id=333_002,
        full_name="e/demo",
        index_status="ready",
        indexed_commit="a" * 40,
        installation_id=1,
    )
    db_session.add(repo)
    db_session.flush()
    return repo


@requires_docker
def test_run_pytest_full_suite_and_targeted_selector(workspace):
    project = write_project(workspace / "project")

    full = run_pytest(project, NO_DEPS)
    assert (full.passed, full.failed, full.exit_code) == (1, 1, 1)
    assert [f.node_id for f in full.failures] == ["test_calc.py::test_add"]

    targeted = run_pytest(project, NO_DEPS, ["test_calc.py::test_ok"])
    assert targeted.all_passed
    assert (targeted.passed, targeted.failed) == (1, 0)

    missing = run_pytest(project, NO_DEPS, ["test_calc.py::test_nope"])
    assert not missing.all_passed


@requires_docker
@requires_pypi
def test_dependencies_are_installed_once_then_used_offline(workspace, monkeypatch):
    project = write_project(workspace / "project", requirements="six==1.17.0\n")
    (project / "test_six.py").write_text(
        "import six\n\n\ndef test_six():\n    assert six.PY3\n", encoding="utf-8"
    )

    env = prepare_dependencies(project)
    assert not env.cached
    assert (env.site_packages / "six.py").exists()

    # The second call must hit the cache without starting another container.
    def forbidden(*args, **kwargs):
        raise AssertionError("dependencies should have come from the cache")

    monkeypatch.setattr(deps_module, "run_in_sandbox", forbidden)
    again = prepare_dependencies(project)
    assert again.cached
    assert again.site_packages == env.site_packages

    # Tests run with the network off, using the cached packages.
    outcome = run_pytest(project, again, ["test_six.py"])
    assert outcome.all_passed, outcome.output


@requires_docker
def test_unsatisfiable_requirement_fails_cleanly(workspace):
    project = write_project(
        workspace / "project", requirements="definitely-not-a-real-pkg-zzz==9.9\n"
    )
    with pytest.raises(deps_module.DependencyError):
        prepare_dependencies(project)
    assert not any((settings.workspace_dir / "deps").glob("*.building"))


@requires_docker
def test_execution_job_records_a_reproduction(workspace, db_session, repo_row, monkeypatch):
    write_project(settings.workspace_dir / str(repo_row.id))
    monkeypatch.setattr(execution_jobs, "SessionLocal", lambda: nullcontext(db_session))
    execution = TestExecution(
        repository_id=repo_row.id,
        kind="reproduction",
        selectors=["test_calc.py::test_add"],
    )
    db_session.add(execution)
    db_session.flush()

    execution_jobs.run_execution_job(execution.id)

    assert execution.status == "completed"
    assert (execution.failed, execution.passed, execution.exit_code) == (1, 0, 1)
    assert execution.failures[0]["node_id"] == "test_calc.py::test_add"
    assert execution.commit_sha == "a" * 40
    assert "assert" in execution.output


def test_execution_job_records_missing_checkout(workspace, db_session, repo_row, monkeypatch):
    monkeypatch.setattr(execution_jobs, "SessionLocal", lambda: nullcontext(db_session))
    execution = TestExecution(repository_id=repo_row.id, kind="baseline", selectors=[])
    db_session.add(execution)
    db_session.flush()

    execution_jobs.run_execution_job(execution.id)
    assert execution.status == "error"
    assert "checkout is missing" in execution.error


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    ids: list[int] = []
    monkeypatch.setattr("app.api.executions.enqueue_execution", ids.append)
    return ids


def test_baseline_endpoint(db_session, repo_row, queued):
    client = TestClient(app)
    response = client.post(f"/repositories/{repo_row.id}/baseline")
    assert response.status_code == 202
    body = response.json()
    assert (body["kind"], body["status"], body["selectors"]) == ("baseline", "queued", [])
    assert queued == [body["id"]]

    # Only one run at a time per repository.
    assert client.post(f"/repositories/{repo_row.id}/baseline").status_code == 409
    assert client.post("/repositories/999999999/baseline").status_code == 404

    listed = client.get(f"/repositories/{repo_row.id}/executions").json()
    assert [e["id"] for e in listed] == [body["id"]]


def test_baseline_requires_indexed_repository(db_session, repo_row, queued):
    repo_row.index_status = "pending"
    db_session.flush()
    assert TestClient(app).post(f"/repositories/{repo_row.id}/baseline").status_code == 409
    assert queued == []


def test_reproduce_uses_test_named_in_the_issue(db_session, repo_row, queued):
    issue = Issue(
        repository=repo_row,
        number=3,
        title="add is wrong",
        body="`tests/test_calc.py::test_add` fails: expected 3 but got -1.",
    )
    db_session.add(issue)
    db_session.flush()

    client = TestClient(app)
    body = client.post(f"/issues/{issue.id}/reproduce").json()
    assert body["kind"] == "reproduction"
    assert body["selectors"] == ["tests/test_calc.py::test_add"]
    assert body["issue_id"] == issue.id
    assert body["reproduced"] is None  # not finished yet


def test_reproduce_needs_a_target(db_session, repo_row, queued):
    issue = Issue(repository=repo_row, number=4, title="vague", body="something is broken")
    db_session.add(issue)
    db_session.flush()
    client = TestClient(app)

    assert client.post(f"/issues/{issue.id}/reproduce").status_code == 422
    assert queued == []
    rejected = client.post(f"/issues/{issue.id}/reproduce", json={"selectors": ["--rootdir=/"]})
    assert rejected.status_code == 422
    assert queued == []

    explicit = client.post(f"/issues/{issue.id}/reproduce", json={"selectors": ["tests/test_a.py"]})
    assert explicit.status_code == 202
    assert explicit.json()["selectors"] == ["tests/test_a.py"]


def test_reproduced_flag_and_detail_endpoint(db_session, repo_row):
    execution = TestExecution(
        repository_id=repo_row.id,
        kind="reproduction",
        status="completed",
        exit_code=1,
        failed=1,
        selectors=["t.py::test_a"],
        failures=[{"kind": "FAILED", "node_id": "t.py::test_a", "message": "boom"}],
        output="full log",
    )
    db_session.add(execution)
    db_session.flush()
    client = TestClient(app)

    detail = client.get(f"/executions/{execution.id}").json()
    assert detail["reproduced"] is True
    assert detail["output"] == "full log"

    execution.exit_code, execution.failed = 0, 0
    db_session.flush()
    assert client.get(f"/executions/{execution.id}").json()["reproduced"] is False

    execution.exit_code = 5  # nothing collected is not a reproduction
    db_session.flush()
    assert client.get(f"/executions/{execution.id}").json()["reproduced"] is False
    assert client.get("/executions/999999999").status_code == 404
    assert db_session.scalar(select(TestExecution).where(TestExecution.id == execution.id))
