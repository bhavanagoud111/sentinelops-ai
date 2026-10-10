"""In-process token-bucket rate limiter.

Good enough for a single replica; a multi-replica deployment would move this to
Redis or the ingress. Keys are bounded so a flood of distinct clients cannot
exhaust memory.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable


class TokenBucketLimiter:
    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate_per_s <= 0 or burst <= 0:
            raise ValueError("rate and burst must be positive")
        self._rate = rate_per_s
        self._burst = float(burst)
        self._max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: str, cost: float = 1.0) -> bool:
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.pop(key, (self._burst, now))
            tokens = min(self._burst, tokens + (now - last) * self._rate)
            allowed = tokens >= cost
            if allowed:
                tokens -= cost
            self._buckets[key] = (tokens, now)
            while len(self._buckets) > self._max_keys:
                self._buckets.popitem(last=False)
            return allowed
