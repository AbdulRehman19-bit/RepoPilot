import logging
from collections.abc import Callable, Iterable

from sqlalchemy.orm import Session

from app.models import Repository

logger = logging.getLogger(__name__)


def queue_indexing(
    session: Session, repos: Iterable[Repository], enqueue: Callable[[int], None]
) -> list[int]:
    """Queue indexing for repositories not indexed yet (or that failed); skip ones in flight.

    Rows are committed *before* the jobs are enqueued so the worker is guaranteed to see them.
    Returns the ids that were queued.
    """
    pending = {r.id: r for r in repos if r.index_status in ("pending", "failed")}
    for repo in pending.values():
        repo.index_status = "queued"
        repo.index_error = None
    session.commit()

    for repo_id in pending:
        try:
            enqueue(repo_id)
        except Exception:
            logger.exception("could not enqueue indexing for repository %s", repo_id)
            repo = session.get(Repository, repo_id)
            repo.index_status = "failed"
            repo.index_error = "could not enqueue indexing job"
            session.commit()
    return list(pending)
