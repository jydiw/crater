"""Tests for the token-bucket rate limiter."""

from crater.tags.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _limiter(clock: FakeClock) -> RateLimiter:
    return RateLimiter(clock=clock.time, sleep=clock.sleep)


class TestRateLimiter:
    def test_first_acquire_does_not_sleep(self) -> None:
        clock = FakeClock()
        rl = _limiter(clock)
        rl.acquire("acoustid", rps=3.0)
        assert clock.sleeps == []

    def test_second_acquire_sleeps_to_maintain_rps(self) -> None:
        clock = FakeClock()
        rl = _limiter(clock)
        rl.acquire("mb", rps=1.0)
        rl.acquire("mb", rps=1.0)
        assert clock.sleeps == [1.0]

    def test_separate_buckets_do_not_interfere(self) -> None:
        clock = FakeClock()
        rl = _limiter(clock)
        rl.acquire("acoustid", rps=3.0)
        rl.acquire("mb", rps=1.0)
        assert clock.sleeps == []

    def test_time_passing_reduces_sleep(self) -> None:
        clock = FakeClock()
        rl = _limiter(clock)
        rl.acquire("mb", rps=1.0)
        clock.now += 0.4  # 0.4s elapses externally
        rl.acquire("mb", rps=1.0)
        assert clock.sleeps == [0.6]

    def test_zero_rps_is_noop(self) -> None:
        clock = FakeClock()
        rl = _limiter(clock)
        rl.acquire("x", rps=0.0)
        rl.acquire("x", rps=0.0)
        assert clock.sleeps == []
