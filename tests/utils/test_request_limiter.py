import concurrent.futures
import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest

from spotdl.utils.request_limiter import (
    RequestLimiter,
    is_rate_limit_error,
    retry_after_seconds,
)


@pytest.mark.parametrize(
    "message",
    [
        "HTTP Error 429",
        "HTTP Error 403",
        "Sign in to confirm you're not a bot",
        "This content isn't available, try again later",
        "Too Many Requests",
    ],
)
def test_recognizes_wrapped_limits(message):
    error = RuntimeError("wrapped")
    error.__cause__ = RuntimeError(message)
    assert is_rate_limit_error(error)
    assert not is_rate_limit_error(ValueError("Video unavailable"))


def test_retry_after_honors_seconds_and_http_dates():
    error = RuntimeError("429")
    error.response = SimpleNamespace(headers={"Retry-After": "3600"})
    assert retry_after_seconds(error) == 3600
    error.response.headers["Retry-After"] = format_datetime(
        datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True
    )
    assert 118 <= retry_after_seconds(error) <= 120
    error.response.headers["Retry-After"] = "invalid"
    assert retry_after_seconds(error) == 0


def test_cooldown_is_shared_escalates_and_honors_retry_after():
    now = [0.0]
    limiter = RequestLimiter(clock=lambda: now[0])
    error = RuntimeError("429")
    error.response = SimpleNamespace(headers={"Retry-After": "1800"})
    with pytest.raises(RuntimeError):
        with limiter.slot():
            raise error
    assert limiter.state == (0, 1800)
    assert limiter.defer(RuntimeError("429")) == 240
    assert limiter.state == (0, 1800)  # A shorter pause cannot shorten Retry-After.
    now[0] = 1801
    assert limiter.state == (0, 0)
    assert limiter.defer(RuntimeError("429")) == 480
    assert limiter.defer(RuntimeError("429")) == 900


def test_slot_spaces_starts_and_waits_for_global_cooldown(monkeypatch):
    now = [0.0]
    limiter = RequestLimiter(min_interval=5, max_interval=5, clock=lambda: now[0])
    monkeypatch.setattr(
        limiter._condition, "wait", lambda timeout: now.__setitem__(0, now[0] + timeout)
    )
    with limiter.slot():
        assert now[0] == 0
    with limiter.slot():
        assert now[0] == 5
    limiter.defer(RuntimeError("429"))
    with limiter.slot():
        assert now[0] == 125
    assert limiter.state == (0, 0)


def test_slot_limits_concurrency_and_releases_on_errors():
    limiter = RequestLimiter(concurrency=2, min_interval=0, max_interval=0)
    entered = threading.Barrier(3)
    release = threading.Event()
    third_entered = threading.Event()

    def blocking():
        with limiter.slot():
            entered.wait(timeout=3)
            assert release.wait(timeout=3)

    def third():
        with limiter.slot():
            third_entered.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(blocking)
        second = executor.submit(blocking)
        entered.wait(timeout=3)
        future = executor.submit(third)
        try:
            assert limiter.state[0] == 2
            assert not third_entered.wait(timeout=0.05)
        finally:
            release.set()
        first.result(timeout=3)
        second.result(timeout=3)
        future.result(timeout=3)
    assert third_entered.is_set()
    assert limiter.state[0] == 0


def test_skip_cooldown_allows_using_a_different_service():
    from spotdl.utils.request_limiter import RequestCooldownError

    youtube = RequestLimiter()
    archive = RequestLimiter()
    youtube.defer(RuntimeError("HTTP Error 429"))
    with pytest.raises(RequestCooldownError, match="cooldown"):
        with youtube.slot(skip_cooldown=True):
            pytest.fail("YouTube request must not start during its cooldown")
    with archive.slot(skip_cooldown=True):
        assert archive.state[0] == 1
    assert youtube.state[0] == 0


def test_waiting_operations_observe_a_new_cooldown(monkeypatch):
    from spotdl.utils.request_limiter import RequestCooldownError

    limiter = RequestLimiter(min_interval=5, max_interval=5, clock=lambda: 0)
    with limiter.slot():
        pass
    monkeypatch.setattr(
        limiter._condition, "wait", lambda timeout: limiter.defer(RuntimeError("429"))
    )
    with pytest.raises(RequestCooldownError):
        with limiter.slot(skip_cooldown=True):
            pytest.fail("A waiting operation must observe a newly imposed cooldown")
    assert limiter.state[0] == 0
