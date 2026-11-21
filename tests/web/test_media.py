"""Exercise media selection, background jobs and persistent download boundaries."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from spotdl.web import gui_api, media, routes
from tests.web.test_datastar import call_asgi

VIDEO = "https://www.youtube.com/watch?v=BaW_jenozKc"
PLAYLIST = "https://www.youtube.com/playlist?list=PLtest123"
OWNER = "test_browser_123456789"


class DeferredExecutor:
    """Run the real worker explicitly, avoiding timing-dependent assertions."""

    def submit(self, function, *args):
        self.pending = (function, args)

    def shutdown(self, **_kwargs):
        pass


@pytest.fixture
def queue(tmp_path, monkeypatch):
    monkeypatch.setattr(
        media, "ThreadPoolExecutor", lambda **_kwargs: DeferredExecutor()
    )
    manager = media.DownloadManager(tmp_path / "downloads", {})
    manager.item_executor = ThreadPoolExecutor(max_workers=4)
    manager.retry_delay = 0
    manager.limiter = SimpleNamespace(slot=lambda **_kwargs: nullcontext())
    yield manager
    manager.close()


@pytest.mark.parametrize(
    "url,scope,kind,canonical",
    [
        ("https://youtu.be/BaW_jenozKc?si=tracking", "auto", "video", VIDEO),
        ("https://m.youtube.com/shorts/BaW_jenozKc", "auto", "video", VIDEO),
        (
            "https://music.youtube.com/watch?v=BaW_jenozKc&list=PLtest123",
            "auto",
            "video",
            VIDEO,
        ),
        (VIDEO + "&list=PLtest123", "playlist", "playlist", PLAYLIST),
        (PLAYLIST, "auto", "playlist", PLAYLIST),
        (
            "https://open.spotify.com/intl-pt/album/4aawyAB9vmqN3uQ7FjRGTy?si=x",
            "auto",
            "album",
            "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy",
        ),
    ],
)
def test_normalize_selection(url, scope, kind, canonical):
    _, actual_kind, actual_url = media.normalize_url(url, scope)
    assert actual_kind == kind
    assert actual_url == canonical


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://youtube.com.evil.test/watch?v=BaW_jenozKc",
        "https://evil.test/",
        "https://youtube.com:8080/watch?v=BaW_jenozKc",
        "https://user@youtube.com/watch?v=BaW_jenozKc",
        "https://youtube.com/watch?v=bad",
        "https://youtube.com/playlist?list=../../etc",
        "https://open.spotify.com/artist/4aawyAB9vmqN3uQ7FjRGTy",
    ],
)
def test_rejects_unsupported_urls(url):
    with pytest.raises(ValueError):
        media.normalize_url(url)


@pytest.fixture
def youtube(monkeypatch):
    calls = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def extract_info(self, url, download=False):
            calls.append((url, download, self.options))
            item = {"id": "BaW_jenozKc", "title": "Vídeo de teste", "ext": "mp4"}
            if "playlist?" in url:
                return {
                    "title": "Playlist de teste",
                    "playlist_count": 2,
                    "entries": [item, {**item, "id": "abcdefghijk"}],
                }
            item["id"] = parse_qs(urlsplit(url).query)["v"][0]
            if download:
                ext = self.options.get("postprocessors", [{}])[0].get(
                    "preferredcodec", "mp4"
                )
                item["ext"] = ext
                path = Path(self.prepare_filename(item))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"finished media")
                for callback in self.options["progress_hooks"]:
                    callback(
                        {
                            "downloaded_bytes": 10,
                            "total_bytes": 10,
                            "status": "finished",
                        }
                    )
                for callback in self.options["post_hooks"]:
                    callback(str(path))
            return item

        def prepare_filename(self, info):
            return (
                self.options["outtmpl"]
                .replace("%(title).150s", info["title"])
                .replace("%(id)s", info["id"])
                .replace("%(ext)s", info["ext"])
            )

    monkeypatch.setattr(media, "YoutubeDL", FakeYoutubeDL)
    return calls


def test_playlist_preview_only_loads_metadata_and_one_thumbnail(queue, youtube):
    preview = queue.preview(PLAYLIST)
    _, download, options = youtube[0]
    assert download is False
    assert options["skip_download"] is True
    assert options["extract_flat"] == "in_playlist"
    assert options["playlistend"] == 1
    assert preview["count"] == 2
    assert preview["thumbnail"] == "https://i.ytimg.com/vi/BaW_jenozKc/hqdefault.jpg"
    assert not queue.jobs


@pytest.mark.parametrize("media_format", ["mp3", "m4a", "flac", "mp4"])
def test_playlist_job_downloads_all_items_and_persists_files(
    queue, youtube, media_format
):
    job = queue.submit(OWNER, PLAYLIST, "playlist", media_format, "720")
    queue._run(job["id"])
    result = queue.owned_job(job["id"], OWNER)
    assert result["status"] == "completed"
    assert result["completed"] == result["total"] == 2
    assert result["progress"] == 100
    assert len(result["files"]) == 2
    assert all(
        (queue.root / filename).read_bytes() == b"finished media"
        for filename in result["files"]
    )
    downloads = [options for _, download, options in youtube if download]
    assert len(downloads) == 2
    assert "playlistend" not in youtube[0][2]
    if media_format == "mp4":
        assert "[height<=720]" in downloads[0]["format"]
        assert downloads[0]["merge_output_format"] == "mp4"
    else:
        assert downloads[0]["postprocessors"][0]["preferredcodec"] == media_format
    saved = json.loads(queue.history_path.read_text())
    assert saved[0]["status"] == "completed"


def test_duplicate_active_job_is_not_submitted_twice(queue):
    first = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    second = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    assert first["id"] == second["id"]
    assert len(queue.jobs) == 1


def test_cancel_queued_job_does_not_fetch_media(queue, youtube):
    job = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    queue.cancel(job["id"], OWNER)
    queue._run(job["id"])
    assert queue.owned_job(job["id"], OWNER)["status"] == "cancelled"
    assert youtube == []


def test_retry_wait_is_cancellable_and_failure_is_recorded(queue, youtube, monkeypatch):
    job = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    attempts = []

    def fail(*_args):
        attempts.append(1)
        raise RuntimeError("Conteúdo privado")

    monkeypatch.setattr(queue, "_youtube_item", fail)
    queue._run(job["id"])
    result = queue.owned_job(job["id"], OWNER)
    assert len(attempts) == 5
    assert result["status"] == "failed"
    assert result["errors"] == [{"item": 1, "error": "Conteúdo privado"}]


def test_restart_keeps_history_and_marks_interrupted_jobs(queue):
    job = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    restored = media.DownloadManager(queue.root, {})
    try:
        result = restored.owned_job(job["id"], OWNER)
        assert result["status"] == "cancelled"
        assert "Interrompido" in result["message"]
    finally:
        restored.close()


def test_failed_playlist_item_does_not_stop_remaining_items(
    queue, youtube, monkeypatch
):
    job = queue.submit(OWNER, PLAYLIST, "playlist", "mp3", "1080")
    download = queue._youtube_item

    def partially_fail(job_id, item, index):
        if index == 0:
            raise RuntimeError("Vídeo removido")
        download(job_id, item, index)

    monkeypatch.setattr(queue, "_youtube_item", partially_fail)
    monkeypatch.setattr(queue.events[job["id"]], "wait", lambda _delay: False)
    queue._run(job["id"])
    result = queue.owned_job(job["id"], OWNER)
    assert result["status"] == "partial"
    assert result["completed"] == result["total"] == 2
    assert len(result["files"]) == len(result["errors"]) == 1


def test_spotify_download_keeps_existing_files_and_cleans_failed_output(
    queue, monkeypatch
):
    import asyncio

    from tests.test_download_runtime import song

    track = song()
    spotify_url = "https://open.spotify.com/track/4aawyAB9vmqN3uQ7FjRGTy"
    job = queue.submit(OWNER, spotify_url, "auto", "mp3", "1080")
    queue.update(job["id"], total=1)
    expected = (
        queue.root / "Spotify" / spotify_url.rsplit("/", 1)[-1] / "Artist - Song.mp3"
    )

    class FailingDownloader:
        def __init__(self, **_kwargs):
            self.loop = asyncio.new_event_loop()
            self.errors = ["Falha nos metadados"]
            self.progress_handler = SimpleNamespace(close=lambda: None)

        def download_song(self, _song):
            if not expected.exists():
                expected.write_bytes(b"incomplete")
            return _song, None

    monkeypatch.setattr(media, "Downloader", FailingDownloader)
    with pytest.raises(RuntimeError, match="Falha nos metadados"):
        queue._spotify_item(job["id"], track, 0)
    assert not expected.exists()

    # A failed attempt must not delete a file that was already present.
    expected.write_bytes(b"existing")
    with pytest.raises(RuntimeError):
        queue._spotify_item(job["id"], track, 0)
    assert expected.read_bytes() == b"existing"


def test_owners_cannot_access_other_browser_jobs(queue):
    job = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    assert queue.list_jobs("another_browser_123456") == []
    with pytest.raises(KeyError):
        queue.cancel(job["id"], "another_browser_123456")


def test_file_endpoint_confines_paths_and_checks_owner(queue, tmp_path):
    app = FastAPI()
    app.state.media_manager = queue
    request = Request({"type": "http", "app": app})
    job = queue.submit(OWNER, VIDEO, "auto", "mp3", "1080")
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"private")
    queue.jobs[job["id"]]["files"] = [str(outside)]
    for index, owner in [(0, OWNER), (-1, OWNER), (0, "another_browser_123456")]:
        with pytest.raises(HTTPException) as error:
            gui_api.download_file(request, job["id"], index, owner)
        assert error.value.status_code == 404
    allowed = queue.root / "audio.mp3"
    allowed.write_bytes(b"audio")
    queue.jobs[job["id"]]["files"] = [allowed.name]
    assert gui_api.download_file(request, job["id"], 0, OWNER).path == allowed


def test_request_rejects_unexpected_format_and_quality():
    for changes in [
        {"format": "exe"},
        {"quality": "1080];exec"},
        {"owner": "../../etc"},
    ]:
        with pytest.raises(ValidationError):
            gui_api.DownloadRequest(**{"owner": OWNER, "url": VIDEO, **changes})


async def test_graphical_home_and_invalid_preview_http(queue):
    app = FastAPI()
    app.state.media_manager = queue
    app.include_router(routes.router)
    app.include_router(gui_api.router)
    status, _, body = await call_asgi(app, "/")
    assert status == 200
    assert b"Music Downloader" in body
    assert b"studio.js" in body
    assert b"<iframe" not in body
    status, _, body = await call_asgi(
        app, "/api/gui/preview", urlencode({"url": "https://example.com"}).encode()
    )
    assert status == 400
    assert "Link não reconhecido" in json.loads(body)["detail"]
