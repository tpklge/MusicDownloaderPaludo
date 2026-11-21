"""Real-thread tests of the shared song budget, retries and cancellation."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from spotdl.web import media

OWNER = "desktop"
COLLECTION = "https://open.spotify.com/playlist/4aawyAB9vmqN3uQ7FjRGTy"


def eventually(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        threading.Event().wait(0.01)
    assert predicate(), "Background condition was not reached"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    queue = media.DownloadManager(tmp_path, {}, workers=1)

    def collection(url, _kind):
        songs = [
            SimpleNamespace(name=f"Song {i}", url=f"{url}/track/{i}", cover_url="")
            for i in range(6)
        ]
        return {"name": "Playlist"}, songs

    monkeypatch.setattr(media, "spotify_collection", collection)
    yield queue
    queue.close()
    queue.executor.shutdown(wait=True)
    queue.item_executor.shutdown(wait=True)


def record_file(manager, job_id, index):
    path = manager.root / f"{job_id}-{index}.mp3"
    path.write_bytes(b"audio")
    manager._record_file(job_id, str(path), index)


def test_worker_budget_is_global_and_resizes_during_active_playlists(
    manager, monkeypatch
):
    release = threading.Event()
    guard = threading.Lock()
    started = []
    running = 0
    peak = 0

    def download(job_id, _song, index):
        nonlocal running, peak
        with guard:
            running += 1
            peak = max(peak, running)
            started.append((job_id, index, manager.workers, running))
        try:
            while not release.wait(0.02):
                manager._check(job_id, index)
            manager.item_progress(job_id, index, progress=70, message="Convertendo")
            record_file(manager, job_id, index)
        finally:
            with guard:
                running -= 1

    monkeypatch.setattr(manager, "_spotify_item", download)
    first = manager.submit(OWNER, COLLECTION, "auto", "mp3", "1080")
    second = manager.submit(
        OWNER, COLLECTION.replace("4aawy", "5aawy"), "auto", "mp3", "1080"
    )
    eventually(lambda: len(started) == 1)
    assert running == 1
    manager.configure(3, request_interval=0)
    eventually(lambda: running == 3)
    manager.configure(1, request_interval=0)
    count_before = len(started)
    release.set()
    eventually(
        lambda: all(job["status"] in media.TERMINAL for job in manager.list_jobs(OWNER))
    )
    assert peak == 3
    assert len(started) == 12
    assert all(
        count == 1 for _, _, budget, count in started[count_before:] if budget == 1
    )
    assert manager.owned_job(first["id"], OWNER)["status"] == "completed"
    assert manager.owned_job(second["id"], OWNER)["status"] == "completed"
    assert not manager.active_workers


def test_backoff_releases_worker_for_next_song(manager, monkeypatch):
    manager.retry_delay = 0.6
    order = []

    def download(job_id, _song, index):
        order.append(index)
        if index == 0 and order.count(0) == 1:
            raise RuntimeError("Temporary failure")
        record_file(manager, job_id, index)

    monkeypatch.setattr(manager, "_spotify_item", download)
    job = manager.submit(OWNER, COLLECTION, "auto", "mp3", "1080")
    eventually(lambda: manager.owned_job(job["id"], OWNER)["status"] in media.TERMINAL)
    result = manager.owned_job(job["id"], OWNER)
    assert order[:2] == [0, 1]
    assert order[-1] == 0
    assert result["items"][0]["attempt"] == 2
    assert all(row["progress"] == 100 for row in result["items"])


def test_cancel_individual_queued_and_running_songs_keeps_other_items(
    manager, monkeypatch
):
    first_started = threading.Event()
    downloaded = []

    def download(job_id, _song, index):
        if index == 0:
            first_started.set()
            while True:
                manager._check(job_id, index)
                threading.Event().wait(0.01)
        downloaded.append(index)
        record_file(manager, job_id, index)

    monkeypatch.setattr(manager, "_spotify_item", download)
    job = manager.submit(OWNER, COLLECTION, "auto", "mp3", "1080")
    assert first_started.wait(3)
    manager.cancel_item(job["id"], 1, OWNER)
    manager.cancel_item(job["id"], 0, OWNER)
    eventually(lambda: manager.owned_job(job["id"], OWNER)["status"] in media.TERMINAL)
    result = manager.owned_job(job["id"], OWNER)
    assert result["status"] == "partial"
    assert result["items"][0]["status"] == result["items"][1]["status"] == "cancelled"
    assert downloaded == [2, 3, 4, 5]
    assert len(result["files"]) == 4


def test_cancel_during_retry_does_not_wait_for_backoff(manager, monkeypatch):
    manager.retry_delay = 300

    def fail(*_args):
        raise RuntimeError("Rate limited")

    monkeypatch.setattr(manager, "_spotify_item", fail)
    job = manager.submit(OWNER, COLLECTION, "auto", "mp3", "1080")
    eventually(
        lambda: all(
            row["status"] == "waiting"
            for row in manager.owned_job(job["id"], OWNER)["items"]
        )
        and bool(manager.owned_job(job["id"], OWNER)["items"])
    )
    manager.cancel(job["id"], OWNER)
    eventually(
        lambda: manager.owned_job(job["id"], OWNER)["status"] == "cancelled", timeout=1
    )
    assert not manager.active_workers


@pytest.mark.parametrize(
    "options",
    [
        {"workers": 0},
        {"workers": 33},
        {"workers": 1, "retries": 11},
        {"workers": 1, "bitrate": "bogus"},
        {"workers": 1, "youtube_concurrency": 9},
    ],
)
def test_configuration_is_validated(manager, options):
    with pytest.raises(ValueError):
        manager.configure(**options)


def test_shutdown_marks_queued_jobs_cancelled(manager, monkeypatch):
    monkeypatch.setattr(manager.executor, "submit", lambda *_: None)
    job = manager.submit(OWNER, COLLECTION, "auto", "mp3", "1080")
    manager.close()
    assert manager.owned_job(job["id"], OWNER)["status"] == "cancelled"
    with pytest.raises(ValueError, match="encerrada"):
        manager.submit(OWNER, COLLECTION, "auto", "mp3", "1080")
