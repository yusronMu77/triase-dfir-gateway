"""Token bucket dalam memori per kunci (GW§5.5). Direset saat restart; diterima untuk satu proses."""

from __future__ import annotations

import time
from collections.abc import Callable

MAX_KEYS = 10_000


class RateLimiter:
    def __init__(self, capacity: int, per_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(capacity)
        self._rate = capacity / per_seconds
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = self._clock()
        if len(self._buckets) > MAX_KEYS:
            self._buckets = {k: v for k, v in self._buckets.items() if self._level(v, now) < self._capacity}
        tokens = self._level(self._buckets.get(key, (self._capacity, now)), now)
        allowed = tokens >= 1
        self._buckets[key] = (tokens - 1 if allowed else tokens, now)
        return allowed

    def _level(self, bucket: tuple[float, float], now: float) -> float:
        tokens, last = bucket
        return min(self._capacity, tokens + (now - last) * self._rate)
