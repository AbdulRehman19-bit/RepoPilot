import httpx
import pytest
from sqlalchemy import select

from app.config import settings
from app.github import sync as sync_module
from app.github.pulls import GitHubClient, GitHubError
from app.github.sync import sync
from app.models import Issue, Repository, User


class FakeGitHubApp:
    """The three kinds of endpoint the sync reads, answering from plain dicts."""

    def __init__(self):
        self.installations = [{"id": 11, "account": {"id": 1, "login": "octo"}}]
        self.repos = {
            11: [
                {
                    "id": 501,
                    "full_name": "octo/demo",
                    "default_branch": "trunk",
                    "owner": {"id": 1, "login": "octo"},
                }
            ]
        }
        self.issues = {
            "octo/demo": [
                {
                    "number": 1,
                    "title": "add() is wrong",
                    "body": "`tests/test_x.py::test_add` fails",
                },
                {"number": 2, "title": "a pull request", "body": "x", "pull_request": {"url": "u"}},
                {"number": 3, "title": "no body", "body": None},
            ]
        }
        self.fail_issues_for: set[str] = set()
        self.calls: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path, page = request.url.path, int(request.url.params.get("page", 1))
        size = int(request.url.params.get("per_page", 100))
        self.calls.append(f"{request.method} {path}")
        chunk = slice((page - 1) * size, page * size)
        if path == "/app/installations":
            return httpx.Response(200, json=self.installations[chunk])
        if path == "/installation/repositories":
            iid = int(request.headers["x-installation"])
            return httpx.Response(200, json={"repositories": self.repos[iid][chunk]})
        if path.endswith("/issues"):
            name = path.removeprefix("/repos/").removesuffix("/issues")
            if name in self.fail_issues_for:
                return httpx.Response(403, json={"message": "issues are disabled"})
            assert request.url.params["state"] == "open"
            return httpx.Response(200, json=self.issues[name][chunk])
        return httpx.Response(404, json={"message": f"unexpected {path}"})

    def client(self, installation: int | None = None) -> GitHubClient:
        http = httpx.Client(
            base_url="https://api.github.test", transport=httpx.MockTransport(self.handle)
        )
        client = GitHubClient("token", client=http)
        if installation is not None:
            client.headers["x-installation"] = str(installation)
        return client

    def for_installation(self, installation_id: int) -> GitHubClient:
        return self.client(installation_id)


@pytest.fixture
def github():
    return FakeGitHubApp()


def run(session, github, **kwargs):
    queued: list[int] = []
    result = sync(
        session,
        app_client=github.client(),
        installation_client=github.for_installation,
        enqueue=queued.append,
        **kwargs,
    )
    return result, queued


def test_sync_stores_repositories_and_open_issues_and_queues_indexing(db_session, github):
    result, queued = run(db_session, github)

    repo = db_session.scalar(select(Repository).where(Repository.github_id == 501))
    assert (repo.full_name, repo.installation_id, repo.default_branch) == ("octo/demo", 11, "trunk")
    assert db_session.scalar(select(User).where(User.github_id == 1)).login == "octo"
    issues = {
        i.number: i for i in db_session.scalars(select(Issue).where(Issue.repository_id == repo.id))
    }
    assert set(issues) == {1, 3}  # the pull request is not a bug report
    assert issues[3].body == ""
    assert issues[1].body.startswith("`tests/test_x.py::test_add`")
    assert (result.installations, result.repositories, result.issues) == (1, ["octo/demo"], 2)
    assert queued == [repo.id] and result.queued == [repo.id]
    assert repo.index_status == "queued"
    assert result.problems == []


def test_running_it_again_updates_instead_of_duplicating(db_session, github):
    run(db_session, github)
    github.issues["octo/demo"][0]["title"] = "add() is still wrong"
    github.issues["octo/demo"].append({"number": 4, "title": "new one", "body": "b"})
    _, queued = run(db_session, github)

    repo = db_session.scalar(select(Repository).where(Repository.github_id == 501))
    issues = db_session.scalars(select(Issue).where(Issue.repository_id == repo.id)).all()
    assert sorted(i.number for i in issues) == [1, 3, 4]
    assert next(i for i in issues if i.number == 1).title == "add() is still wrong"
    assert queued == []  # already queued/indexed: not requeued
    assert db_session.scalars(select(Repository).where(Repository.github_id == 501)).all() == [repo]


def test_a_repository_that_failed_indexing_is_requeued(db_session, github):
    run(db_session, github)
    repo = db_session.scalar(select(Repository).where(Repository.github_id == 501))
    repo.index_status = "failed"
    db_session.flush()
    _, queued = run(db_session, github)
    assert queued == [repo.id]


def test_no_index_flag_stores_without_queueing(db_session, github):
    result, queued = run(db_session, github, index=False)
    assert result.repositories == ["octo/demo"] and queued == []
    repo = db_session.scalar(select(Repository).where(Repository.github_id == 501))
    assert repo.index_status == "pending"


def test_one_broken_installation_does_not_stop_the_others(db_session, github):
    github.installations.append({"id": 22, "account": {"id": 2, "login": "other"}})
    github.repos[22] = [
        {
            "id": 502,
            "full_name": "other/lib",
            "default_branch": "main",
            "owner": {"id": 2, "login": "other"},
        }
    ]
    github.issues["other/lib"] = []

    def flaky(installation_id: int) -> GitHubClient:
        if installation_id == 11:
            raise GitHubError("GitHub 401: Bad credentials", 401)
        return github.for_installation(installation_id)

    result = sync(
        db_session, app_client=github.client(), installation_client=flaky, enqueue=lambda i: None
    )
    assert result.repositories == ["other/lib"]
    assert len(result.problems) == 1 and "installation 11" in result.problems[0]


def test_a_repository_whose_issues_cannot_be_read_is_still_recorded(db_session, github):
    github.fail_issues_for.add("octo/demo")
    result, _ = run(db_session, github)
    assert result.repositories == ["octo/demo"] and result.issues == 0
    assert any("octo/demo issues" in p and "disabled" in p for p in result.problems)


def test_results_are_paged(db_session, github, monkeypatch):
    monkeypatch.setattr(sync_module, "PAGE_SIZE", 2)
    github.issues["octo/demo"] = [{"number": n, "title": f"t{n}", "body": ""} for n in range(1, 6)]
    result, _ = run(db_session, github)
    assert result.issues == 5
    assert sum(c == "GET /repos/octo/demo/issues" for c in github.calls) == 3


def test_an_account_with_no_installations_is_reported_not_an_error(db_session, github):
    github.installations.clear()
    result, queued = run(db_session, github)
    assert result.installations == 0 and result.repositories == [] and queued == []


def test_the_command_refuses_to_run_without_a_configured_app(monkeypatch, capsys):
    monkeypatch.setattr(settings, "github_app_id", "")
    assert sync_module.main() == 2
    assert "not configured" in capsys.readouterr().err
