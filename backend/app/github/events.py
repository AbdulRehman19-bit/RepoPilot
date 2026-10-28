from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Issue, Repository, User

ISSUE_ACTIONS = {"opened", "edited", "reopened"}


def upsert_user(session: Session, github_id: int, login: str) -> User:
    user = session.scalar(select(User).where(User.github_id == github_id))
    if user is None:
        user = User(github_id=github_id, login=login)
        session.add(user)
    else:
        user.login = login
    return user


def upsert_repository(
    session: Session,
    owner: User,
    github_id: int,
    full_name: str,
    installation_id: int | None,
    default_branch: str | None = None,
) -> Repository:
    repo = session.scalar(select(Repository).where(Repository.github_id == github_id))
    if repo is None:
        repo = Repository(owner=owner, github_id=github_id, full_name=full_name)
        session.add(repo)
    repo.full_name = full_name
    repo.installation_id = installation_id
    if default_branch:
        repo.default_branch = default_branch
    elif not repo.default_branch:
        repo.default_branch = "main"
    return repo


def _disconnect(session: Session, github_ids: list[int]) -> None:
    for repo in session.scalars(select(Repository).where(Repository.github_id.in_(github_ids))):
        repo.installation_id = None


def handle_installation(session: Session, payload: dict) -> bool:
    action = payload.get("action")
    installation = payload["installation"]
    account = installation["account"]
    owner = upsert_user(session, account["id"], account["login"])

    if action == "created":
        for repo in payload.get("repositories", []):
            upsert_repository(session, owner, repo["id"], repo["full_name"], installation["id"])
        return True
    if action == "deleted":
        _disconnect(session, [repo["id"] for repo in payload.get("repositories", [])])
        for repo in session.scalars(
            select(Repository).where(Repository.installation_id == installation["id"])
        ):
            repo.installation_id = None
        return True
    return False


def handle_installation_repositories(session: Session, payload: dict) -> bool:
    installation = payload["installation"]
    account = installation["account"]
    owner = upsert_user(session, account["id"], account["login"])

    for repo in payload.get("repositories_added", []):
        upsert_repository(session, owner, repo["id"], repo["full_name"], installation["id"])
    _disconnect(session, [repo["id"] for repo in payload.get("repositories_removed", [])])
    return payload.get("action") in {"added", "removed"}


def handle_issues(session: Session, payload: dict) -> bool:
    if payload.get("action") not in ISSUE_ACTIONS:
        return False

    repo_data = payload["repository"]
    owner_data = repo_data["owner"]
    owner = upsert_user(session, owner_data["id"], owner_data["login"])
    repo = upsert_repository(
        session,
        owner,
        repo_data["id"],
        repo_data["full_name"],
        (payload.get("installation") or {}).get("id"),
        repo_data.get("default_branch"),
    )
    session.flush()

    issue_data = payload["issue"]
    issue = session.scalar(
        select(Issue).where(Issue.repository_id == repo.id, Issue.number == issue_data["number"])
    )
    if issue is None:
        issue = Issue(repository=repo, number=issue_data["number"], title="")
        session.add(issue)
    issue.title = issue_data["title"]
    issue.body = issue_data.get("body") or ""
    return True


HANDLERS = {
    "installation": handle_installation,
    "installation_repositories": handle_installation_repositories,
    "issues": handle_issues,
}


def handle_event(session: Session, event: str, payload: dict) -> bool:
    """Persist a webhook event. Returns False when the event is ignored."""
    handler = HANDLERS.get(event)
    if handler is None:
        return False
    return handler(session, payload)
