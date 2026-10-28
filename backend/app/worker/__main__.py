import sys

from redis import Redis
from rq import SimpleWorker, Worker

from app.config import settings

if __name__ == "__main__":
    connection = Redis.from_url(settings.redis_url)
    # Worker forks per job, which Windows does not support; SimpleWorker runs jobs in-process.
    worker_class = SimpleWorker if sys.platform == "win32" else Worker
    worker_class([settings.queue_name], connection=connection).work()
