from redis import Redis
from rq import Queue

from app.config import settings


def get_queue() -> Queue:
    return Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))


def enqueue_index(repo_id: int) -> None:
    from app.worker.jobs import index_repository_job

    get_queue().enqueue(index_repository_job, repo_id, job_timeout=900)
