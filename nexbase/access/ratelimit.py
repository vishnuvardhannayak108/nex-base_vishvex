"""Thread-safe rate limiting, global and per-host."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit


class RateLimiter:
    """Enforces a minimum interval between permitted operations."""

    def __init__(self, rate_per_second: float = 2.0) -> None:
        self._min_interval = (1.0 / rate_per_second) if rate_per_second > 0 else 0.0
        self._last: float = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_for = self._last + self._min_interval - now
            if wait_for > 0:
                time.sleep(wait_for)
            self._last = time.monotonic()


class HostRateLimiter:
    """Applies a global rate limit plus an independent per-host limit.

    A single global limiter is not enough: hammering one domain at the global
    rate is exactly what gets an IP blocked. A shared instance is used across
    the whole pipeline run so limits are not reset per company.
    """

    def __init__(
        self,
        rate_per_second: float = 2.0,
        per_host_rate_per_second: float = 0.5,
    ) -> None:
        self._global = RateLimiter(rate_per_second)
        self._per_host_interval = (
            (1.0 / per_host_rate_per_second) if per_host_rate_per_second > 0 else 0.0
        )
        self._host_last: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def host_of(url: str) -> str:
        try:
            return (urlsplit(url).netloc or "").lower()
        except ValueError:
            return ""

    def wait(self, url: str) -> None:
        self._global.wait()
        if self._per_host_interval <= 0:
            return
        host = self.host_of(url)
        if not host:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._host_last.get(host, 0.0)
                wait_for = last + self._per_host_interval - now
                if wait_for <= 0:
                    self._host_last[host] = now
                    return
            time.sleep(wait_for)


class HostCircuitBreaker:
    """Stops hammering a host that keeps failing.

    Contact discovery probes ~11 speculative paths per company. When a site is
    down or blackholing connections, every probe costs a full timeout, so one
    dead host can consume an entire run's time budget. After
    ``failure_threshold`` consecutive failures the host is skipped for the rest
    of the run; any success resets the counter.
    """

    def __init__(self, failure_threshold: int = 3) -> None:
        self.failure_threshold = failure_threshold
        self._failures: dict[str, int] = {}
        self._lock = threading.Lock()

    def is_open(self, url: str) -> bool:
        host = HostRateLimiter.host_of(url)
        with self._lock:
            return self._failures.get(host, 0) >= self.failure_threshold

    def record_failure(self, url: str) -> None:
        host = HostRateLimiter.host_of(url)
        with self._lock:
            self._failures[host] = self._failures.get(host, 0) + 1

    def record_success(self, url: str) -> None:
        host = HostRateLimiter.host_of(url)
        with self._lock:
            self._failures.pop(host, None)

    def failures(self, url: str) -> int:
        host = HostRateLimiter.host_of(url)
        with self._lock:
            return self._failures.get(host, 0)


class FallbackBudget:
    """Caps expensive browser fallbacks per run.

    Two limits, doing different jobs:

    * ``max_total`` bounds cost across the whole run.
    * ``host_max`` lets one host be rescued several times before its cooldown
      starts, so a site that blocks Scrapling on every page can still be read.
      Without this, the first fallback locked the host for the cooldown and the
      rest of that company's site was abandoned - measured at 17 of 20 pages
      lost on a live, in-ICP company.

    The cooldown still exists; it just begins once a host has used its
    allowance rather than after a single page.
    """

    def __init__(self, max_total: int = 25, host_cooldown_seconds: float = 300.0,
                 host_max: int = 6) -> None:
        self.max_total = max_total
        self.host_cooldown_seconds = host_cooldown_seconds
        self.host_max = host_max
        self.used = 0
        self._host_last: dict[str, float] = {}
        self._host_used: dict[str, int] = {}
        self._lock = threading.Lock()

    def host_used(self, url: str) -> int:
        return self._host_used.get(HostRateLimiter.host_of(url), 0)

    def allows(self, url: str) -> tuple[bool, str | None]:
        host = HostRateLimiter.host_of(url)
        with self._lock:
            if self.used >= self.max_total:
                return False, "BUDGET_EXHAUSTED"
            if self._host_used.get(host, 0) < self.host_max:
                return True, None
            # Allowance spent: fall back to the cooldown.
            last = self._host_last.get(host)
            if last is not None and (time.monotonic() - last) < self.host_cooldown_seconds:
                return False, "HOST_COOLDOWN"
            return True, None

    def consume(self, url: str) -> None:
        host = HostRateLimiter.host_of(url)
        with self._lock:
            self.used += 1
            self._host_last[host] = time.monotonic()
            self._host_used[host] = self._host_used.get(host, 0) + 1
