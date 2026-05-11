"""Token-bucket rate limiter keyed by bucket name.

Used to stay within AcoustID's 3 req/sec and MusicBrainz's 1 req/sec policies.
A single `RateLimiter` instance is shared across a lookup run; `acquire(name,
rps)` blocks until the next token is available for that bucket.

Clock/sleep are injectable for tests that need to verify spacing without real
wall-clock waits.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class RateLimiter:
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _next_allowed: dict[str, float] = field(default_factory=dict)

    def acquire(self, name: str, rps: float) -> None:
        if rps <= 0:
            return
        interval = 1.0 / rps
        now = self.clock()
        next_at = self._next_allowed.get(name, 0.0)
        if now < next_at:
            self.sleep(next_at - now)
            now = next_at
        self._next_allowed[name] = now + interval
