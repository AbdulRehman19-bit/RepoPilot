"""Pull what the GitHub App can see, instead of waiting for webhooks.

    uv run python -m app.github.sync

Lists the app's installations, their repositories and each repository's open issues, stores them
(the same rows the webhooks create), and queues indexing. It needs no public URL, so a local
pilot works without a tunnel, and it also backfills issues opened before the app was installed.
Run it again any time; it only adds or updates.
"""

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import SessionLocal
from app.github.auth import create_app_jwt
from app.github.events import upsert_repository, upsert_user
from app.github.indexing import queue_indexing
from app.github.pulls import GitHubClient, get_github_client
from app.models import Issue, Repository
from app.worker.queue import enqueue_index

PAGE_SIZE = 100
MAX_PAGES = 10  # bounds the work for an account with a huge number of repositories or issues
MAX_ISSUES_PER_REPO = 200


@dataclass
class SyncResult:
    installations: int = 0
    repositories: list[str] = field(default_factory=list)
    issues: int = 0
    queued: list[int] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _pages(client: GitHubClient, path: str, key: str | None = None, **params) -> list[dict]:
    items: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        data = client.request("GET", path, params={"per_page": PAGE_SIZE, "page": page, **params})
        batch = data.get(key, []) if key else data
        items += batch
        if len(batch) < PAGE_SIZE:
            break
    return items


def _store_issues(session: Session, repo: Repository, issues: list[dict]) -> int:
    count = 0
    for data in issues:
        if "pull_request" in data:  # GitHub lists pull requests as issues; they are not bug reports
            continue
        issue = session.scalar(
            select(Issue).where(Issue.repository_id == repo.id, Issue.number == data["number"])
        )
        if issue is None:
            issue = Issue(repository=repo, number=data["number"], title="")
            session.add(issue)
        issue.title = data["title"]
        issue.body = data.get("body") or ""
        count += 1
    return count


def sync(
    session: Session,
    *,
    app_client: GitHubClient | None = None,
    installation_client: Callable[[int], GitHubClient] = get_github_client,
    enqueue: Callable[[int], None] = enqueue_index,
    index: bool = True,
) -> SyncResult:
    result = SyncResult()
    if app_client is None:
        token = create_app_jwt(settings.github_app_id, settings.github_private_key())
        app_client = GitHubClient(token)

    installations = _pages(app_client, "/app/installations")
    result.installations = len(installations)
    touched: list[Repository] = []

    for installation in installations:
        try:
            client = installation_client(installation["id"])
            repos = _pages(client, "/installation/repositories", "repositories")
        except Exception as exc:  # noqa: BLE001 - report it and carry on with the other installations
            result.problems.append(f"installation {installation['id']}: {exc}")
            continue
        for data in repos:
            owner = upsert_user(session, data["owner"]["id"], data["owner"]["login"])
            repo = upsert_repository(
                session,
                owner,
                data["id"],
                data["full_name"],
                installation["id"],
                data.get("default_branch"),
            )
            session.flush()
            try:
                issues = _pages(client, f"/repos/{data['full_name']}/issues", state="open")[
                    :MAX_ISSUES_PER_REPO
                ]
            except Exception as exc:  # noqa: BLE001
                result.problems.append(f"{data['full_name']} issues: {exc}")
                issues = []
            result.issues += _store_issues(session, repo, issues)
            result.repositories.append(data["full_name"])
            touched.append(repo)

    session.commit()
    if index:
        result.queued = queue_indexing(session, touched, enqueue)
    return result


def main() -> int:
    if not settings.github_app_id or not settings.github_private_key_path:
        print(
            "GitHub App is not configured (REPOPILOT_GITHUB_APP_ID / _PRIVATE_KEY_PATH).",
            file=sys.stderr,
        )
        return 2
    with SessionLocal() as session:
        result = sync(session, index="--no-index" not in sys.argv[1:])
    print(f"installations: {result.installations}")
    print(f"repositories:  {', '.join(result.repositories) or 'none'}")
    print(f"open issues:   {result.issues}")
    print(
        f"indexing queued for {len(result.queued)} repositor{'y' if len(result.queued) == 1 else 'ies'}"
    )
    for problem in result.problems:
        print(f"PROBLEM: {problem}", file=sys.stderr)
    if not result.installations:
        print("The app is not installed anywhere yet: open its page on GitHub and choose Install.")
    return 1 if result.problems else 0


if __name__ == "__main__":
    sys.exit(main())
