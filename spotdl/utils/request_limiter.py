"""Shared pacing for YouTube searches and downloads across worker threads."""

import math
import random
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator, Optional


def exception_chain(error: BaseException) -> Iterator[BaseException]:
    """Include wrapped HTTP errors without looping over cyclic causes."""
    seen = set()
    current: Optional[BaseException] = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def is_rate_limit_error(error: BaseException) -> bool:
    """Recognize server limits and YouTube's equivalent bot/session errors."""
    for cause in exception_chain(error):
        response = getattr(cause, "response", None)
        status = getattr(cause, "status", None) or getattr(
            response, "status_code", None
        )
        message = str(cause).lower()
        if status in (403, 429) or re.search(r"\b(?:403|429)\b", message):
            return True
        if any(
            text in message
            for text in (
                "too many requests",
                "not a bot",
                "captcha",
                "try again later",
                "request rate limit",
            )
        ):
            return True
    return False


def retry_after_seconds(error: BaseException) -> float:
    """Honor both seconds and HTTP dates in a wrapped Retry-After header."""
    delay = 0.0
    for cause in exception_chain(error):
        response = getattr(cause, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            headers = getattr(cause, "headers", {})
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            continue
        try:
            seconds = float(value)
        except (ValueError, TypeError):
            try:
                when = parsedate_to_datetime(str(value))
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (when - datetime.now(timezone.utc)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    return delay


class RequestCooldownError(Exception):
    """The service is cooling down; a different source may still be used."""


class RequestLimiter:
    """Limit concurrent operations, space their starts and share server cooldowns.

    One operation may perform several HTTP requests; yt-dlp's sleep_requests
    additionally spaces its internal extraction requests. Conversions do not
    hold a slot. A single limiter must be shared by all download workers.
    """

    def __init__(
        self,
        concurrency: int = 2,
        min_interval: float = 5,
        max_interval: float = 10,
        cooldown: float = 120,
        max_cooldown: float = 900,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if concurrency < 1 or not 0 <= min_interval <= max_interval:
            raise ValueError("Invalid concurrency or pacing interval")
        if not 0 < cooldown <= max_cooldown:
            raise ValueError("Invalid cooldown interval")
        self.concurrency = concurrency
        self.min_interval = min_interval
        self.max_interval = max_interval
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self.clock = clock
        self._condition = threading.Condition()
        self._active = 0
        self._next_start = 0.0
        self._blocked_until = 0.0
        self._blocks = 0

    @property
    def state(self) -> tuple[int, float]:
        """Return active operations and remaining cooldown seconds."""
        with self._condition:
            return self._active, max(0.0, self._blocked_until - self.clock())

    def configure(
        self, concurrency: int, min_interval: float, max_interval: float
    ) -> None:
        """Apply live pacing changes without replacing active slots or cooldowns."""
        if concurrency < 1 or not 0 <= min_interval <= max_interval:
            raise ValueError("Invalid concurrency or pacing interval")
        with self._condition:
            self.concurrency = concurrency
            self.min_interval = min_interval
            self.max_interval = max_interval
            self._condition.notify_all()

    def defer(self, error: BaseException) -> float:
        """Pause every worker, escalating repeated blocking responses."""
        with self._condition:
            self._blocks += 1
            delay = min(
                self.max_cooldown, self.cooldown * 2 ** min(self._blocks - 1, 16)
            )
            delay = max(delay, retry_after_seconds(error))
            self._blocked_until = max(self._blocked_until, self.clock() + delay)
            self._condition.notify_all()
            return delay

    @contextmanager
    def slot(self, skip_cooldown: bool = False) -> Iterator[None]:
        """Wait for a paced slot and release it even on failed extraction."""
        with self._condition:
            while True:
                if skip_cooldown and self._blocked_until > self.clock():
                    remaining = math.ceil(self._blocked_until - self.clock())
                    raise RequestCooldownError(
                        f"Service cooldown: retry in {remaining}s"
                    )
                delay = max(self._next_start, self._blocked_until) - self.clock()
                if self._active < self.concurrency and delay <= 0:
                    self._active += 1
                    self._next_start = self.clock() + random.uniform(
                        self.min_interval, self.max_interval
                    )
                    break
                self._condition.wait(timeout=min(delay, 1) if delay > 0 else 1)
        try:
            yield
        except Exception as error:
            if is_rate_limit_error(error):
                self.defer(error)
            raise
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
