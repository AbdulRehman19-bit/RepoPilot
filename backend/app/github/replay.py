"""Refuse to process the same signed webhook body twice.

GitHub signs the body but not the headers, so the delivery id can be swapped by anyone replaying a
captured request; the body hash cannot. Without this, replaying an old `issues` event would
overwrite an issue with its earlier text. The window is `TTL_SECONDS`: an older capture still
verifies, and GitHub gives us no timestamp to reject it by.
"""

import hashlib
import logging

from redis import Redis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger(__name__)
TTL_SECONDS = 24 * 3600


def _key(body: bytes) -> str:
    return f"{settings.queue_name}:webhook:{hashlib.sha256(body).hexdigest()}"


def claim(body: bytes) -> bool:
    """True if this body has not been seen recently (and is now marked as seen).

    Fails open if Redis is unreachable: a missed duplicate is far less harmful than dropping every
    legitimate event, and indexing needs Redis anyway.
    """
    try:
        return bool(Redis.from_url(settings.redis_url).set(_key(body), 1, nx=True, ex=TTL_SECONDS))
    except RedisError:
        logger.warning("replay protection unavailable: could not reach Redis")
        return True


def release(body: bytes) -> None:
    """Forget a body whose processing failed, so GitHub's redelivery is accepted."""
    try:
        Redis.from_url(settings.redis_url).delete(_key(body))
    except RedisError:
        logger.warning("could not release webhook claim: Redis unreachable")
