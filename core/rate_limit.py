from __future__ import annotations

import time
from collections import defaultdict

from core.config import get_settings


class RateLimiter:
    """Fixed-window limiter. Redis is used when configured; memory is the development fallback."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or ""
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._redis = None

    async def _client(self):
        if not self.redis_url:
            return None
        if self._redis is None:
            try:
                from redis.asyncio import Redis

                self._redis = Redis.from_url(self.redis_url, socket_connect_timeout=0.3)
                await self._redis.ping()
            except Exception:
                self._redis = None
                return None
        return self._redis

    async def allow(self, key: str, limit: int, window_sec: int) -> bool:
        client = await self._client()
        if client is not None:
            try:
                redis_key = f"rl:{key}"
                count = await client.incr(redis_key)
                if count == 1:
                    await client.expire(redis_key, window_sec)
                return int(count) <= limit
            except Exception:
                self._redis = None
        now = time.monotonic()
        bucket = [stamp for stamp in self._hits[key] if now - stamp < window_sec]
        if len(bucket) >= limit:
            self._hits[key] = bucket
            return False
        bucket.append(now)
        self._hits[key] = bucket
        return True


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(get_settings().redis_url or None)
    return _limiter
