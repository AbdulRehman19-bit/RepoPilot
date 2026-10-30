"""Fixed-window rate limits for the routes that cost real work (sandbox runs, model calls).

Counters live in Redis so every API process shares them. If Redis is unreachable the limit fails
open (queueing a job needs Redis anyway, and a broken counter should not take the API down).
"""

import logging
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request
from redis import Redis
from redis.exceptions import RedisError

from app.api.security import Principal, PrincipalDep
from app.config import settings

logger = logging.getLogger(__name__)
HOUR, MINUTE = 3600, 60


def hit(key: str, limit: int, window: int) -> int | None:
    """Count one use of `key`. Returns None if allowed, else the seconds until it resets."""
    try:
        redis = Redis.from_url(settings.redis_url)
        full_key = f"{settings.queue_name}:rate:{key}"
        count = redis.incr(full_key)
        ttl = redis.ttl(full_key)
        if ttl < 0:  # first use, or a counter that somehow lost its expiry
            redis.expire(full_key, window)
            ttl = window
    except RedisError:
        logger.warning("rate limiting unavailable: could not reach Redis")
        return None
    return None if count <= limit else max(ttl, 1)


def _refuse(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail=f"rate limit reached; try again in {retry_after} seconds",
        headers={"Retry-After": str(retry_after)},
    )


def _who(principal: Principal) -> str:
    return f"{principal.kind}:{principal.login}" if principal.login else principal.kind


def per_caller(bucket: str, setting: str) -> Callable:
    """A dependency allowing each caller `settings.<setting>` uses of `bucket` per hour."""

    def dependency(principal: PrincipalDep) -> None:
        limit = getattr(settings, setting)
        if limit > 0 and (wait := hit(f"{bucket}:{_who(principal)}", limit, HOUR)) is not None:
            raise _refuse(wait)

    return Depends(dependency)


def per_ip(bucket: str, setting: str) -> Callable:
    """Like `per_caller` for unauthenticated routes: per client address, per minute."""

    def dependency(request: Request) -> None:
        limit = getattr(settings, setting)
        ip = request.client.host if request.client else "unknown"
        if limit > 0 and (wait := hit(f"{bucket}:ip:{ip}", limit, MINUTE)) is not None:
            raise _refuse(wait)

    return Depends(dependency)
