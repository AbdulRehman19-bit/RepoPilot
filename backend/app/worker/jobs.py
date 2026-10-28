from app.config import settings
from app.db import SessionLocal
from app.github.auth import mint_installation_token
from app.indexing.indexer import index_repository
from app.indexing.workspace import clone_repository, remove_tree
from app.models import Repository


def clone_source(repo: Repository) -> tuple[str, str | None]:
    """The URL to clone and, for installed repos, a short-lived installation token."""
    url = f"{settings.github_clone_base}/{repo.full_name}.git"
    token = None
    if repo.installation_id and settings.github_app_id:
        token = mint_installation_token(repo.installation_id).token
    return url, token


def index_repository_job(repo_id: int) -> None:
    """Clone a repository into the workspace and (re)build its code index."""
    with SessionLocal() as session:
        repo = session.get(Repository, repo_id)
        if repo is None:
            return
        repo.index_status = "indexing"
        repo.index_error = None
        session.commit()

        try:
            url, token = clone_source(repo)
            dest = settings.workspace_dir / str(repo.id)
            remove_tree(dest)
            sha = clone_repository(url, dest, token)
            index_repository(session, repo, dest, sha)
            repo.index_status = "ready"
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded on the repo
            session.rollback()
            repo = session.get(Repository, repo_id)
            repo.index_status = "failed"
            repo.index_error = str(exc)[:2000]
        session.commit()
