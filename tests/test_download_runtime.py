"""Regression coverage for provider fallback and concurrent download progress."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from spotdl.download.downloader import Downloader
from spotdl.download.progress_handler import (
    ProgressHandler,
    ProgressTracker,
    SongTracker,
)
from spotdl.providers.audio.base import AudioProvider
from spotdl.types.song import Song
from spotdl.utils.request_limiter import RequestLimiter


def song(name="Song"):
    return Song.from_dict(
        {
            "name": name,
            "artist": "Artist",
            "artists": ["Artist"],
            "url": f"spotify:{name}",
            "song_id": name,
            "genres": [],
            "disc_count": 1,
            "tracks_count": 1,
            "track_number": 1,
            "album_id": "album",
            "album_artist": "Artist",
            "disc_number": 1,
            "album_name": "Album",
            "duration": 180,
            "year": 2026,
            "date": "2026-01-01",
            "explicit": False,
            "publisher": "Publisher",
            "isrc": None,
            "cover_url": None,
            "copyright_text": None,
        }
    )


def test_provider_error_falls_back_to_normal_youtube(mocker):
    downloader = Downloader.__new__(Downloader)
    first = mocker.Mock()
    first.search.side_effect = RuntimeError("YouTube Music unavailable")
    second = mocker.Mock()
    second.search.return_value = "https://www.youtube.com/watch?v=video"
    downloader.audio_providers = [first, second]
    downloader.settings = {"only_verified_results": False}

    assert downloader.search(song()) == second.search.return_value
    second.search.assert_called_once()


def test_provider_rate_limit_falls_back_to_another_service(mocker):
    downloader = Downloader.__new__(Downloader)
    first = mocker.Mock()
    first.search.side_effect = RuntimeError("HTTP Error 429")
    second = mocker.Mock()
    second.search.return_value = "https://archive.org/download/album/song.mp3"
    downloader.audio_providers = [first, second]
    downloader.settings = {"only_verified_results": False}

    assert downloader.search(song()) == second.search.return_value
    second.search.assert_called_once()


def test_progress_trackers_do_not_share_song_state():
    first, second = ProgressTracker(), ProgressTracker()
    first.add(song())
    assert first.songs
    assert not second.songs


def test_simple_progress_callback_receives_real_download_and_conversion_progress():
    updates = []
    parent = ProgressHandler(
        simple_tui=True,
        update_callback=lambda tracker, _: updates.append(tracker.progress),
    )
    tracker = SongTracker(parent, song())
    tracker.yt_dlp_progress_hook(
        {"status": "downloading", "total_bytes": 100, "downloaded_bytes": 50}
    )
    tracker.ffmpeg_progress_hook(50)
    assert updates == [55, 82]


@pytest.mark.asyncio
async def test_concurrent_downloads_have_isolated_temp_files(mocker, tmp_path):
    mocker.patch("spotdl.download.downloader.get_temp_path", return_value=tmp_path)
    mocker.patch("spotdl.download.downloader.shutil.which", return_value="ffmpeg")
    mocker.patch("spotdl.download.downloader.embed_metadata")
    paths = []

    def download(provider, url, download=False):
        template = provider.audio_handler.params["outtmpl"]["default"]
        path = Path(template % {"id": "same-video", "ext": "mp3"})
        paths.append(path)
        path.write_bytes(b"audio")
        return {"id": "same-video", "ext": "mp3"}

    mocker.patch.object(AudioProvider, "get_download_metadata", download)
    limiter = RequestLimiter(min_interval=0, max_interval=0)
    downloader = Downloader(
        settings={
            "audio_providers": ["youtube"],
            "lyrics_providers": [],
            "simple_tui": True,
            "bitrate": "disable",
            "output": str(tmp_path / "{title}.{output-ext}"),
        },
        loop=asyncio.get_running_loop(),
        request_limiter=limiter,
    )
    mocker.patch.object(
        downloader, "search", return_value="https://www.youtube.com/watch?v=same-video"
    )
    try:
        results = await asyncio.gather(
            *[
                downloader.async_search_and_download(song(name))
                for name in ("First", "Second")
            ]
        )
        assert not downloader.errors
        assert all(
            path is not None and path.read_bytes() == b"audio" for _, path in results
        )
        assert len(paths) == 2
        assert paths[0].parent != paths[1].parent
        assert all(not path.parent.exists() for path in paths)
        assert downloader.audio_providers[0].request_limiter is limiter
    finally:
        for provider in downloader.audio_providers:
            provider.audio_handler.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures, expected",
    [
        ((), ["youtube"]),
        (("youtube",), ["youtube", "internet-archive"]),
        (
            ("youtube", "internet-archive"),
            ["youtube", "internet-archive", "soundcloud"],
        ),
    ],
)
async def test_search_and_download_fallback_preserves_service_priority(
    mocker, tmp_path, failures, expected
):
    from spotdl.providers.audio import InternetArchive, SoundCloud, YouTube

    mocker.patch("spotdl.download.downloader.get_temp_path", return_value=tmp_path)
    mocker.patch("spotdl.download.downloader.shutil.which", return_value="ffmpeg")
    mocker.patch("spotdl.download.downloader.embed_metadata")
    urls = {
        "youtube": "https://www.youtube.com/watch?v=video",
        "internet-archive": "https://archive.org/download/album/song.mp3",
        "soundcloud": "https://soundcloud.com/artist/song",
    }
    events = []
    for name, provider_class in [
        ("youtube", YouTube),
        ("internet-archive", InternetArchive),
        ("soundcloud", SoundCloud),
    ]:

        def search(_provider, _song, _only_verified=False, service=name):
            events.append(("search", service))
            return urls[service]

        mocker.patch.object(provider_class, "search", search)

    limiters = {name: RequestLimiter(min_interval=0, max_interval=0) for name in urls}

    def download(provider, url, download=False):
        service = next(key for key, value in urls.items() if value == url)
        events.append(("download", service))
        assert provider.request_limiter is limiters[service]
        if service in failures:
            raise RuntimeError(f"{service} download unavailable")
        info = {"id": "same-id", "ext": "mp3"}
        Path(provider.audio_handler.prepare_filename(info)).write_bytes(
            b"complete audio"
        )
        return info

    mocker.patch.object(AudioProvider, "get_download_metadata", download)
    downloader = Downloader(
        settings={
            "audio_providers": list(urls),
            "lyrics_providers": [],
            "simple_tui": True,
            "bitrate": "disable",
            "output": str(tmp_path / "{title}.{output-ext}"),
        },
        loop=asyncio.get_running_loop(),
        request_limiter=limiters["youtube"],
        provider_limiters={
            key: value for key, value in limiters.items() if key != "youtube"
        },
    )
    try:
        item, path = await downloader.async_search_and_download(song())
        assert path is not None and path.read_bytes() == b"complete audio"
        assert item.download_url == urls[expected[-1]]
        assert events == [
            event
            for name in expected
            for event in (("search", name), ("download", name))
        ]
        assert not downloader.errors
    finally:
        for provider in downloader.audio_providers:
            provider.audio_handler.close()


@pytest.mark.asyncio
async def test_youtube_cooldown_does_not_prevent_archive_download(mocker, tmp_path):
    from spotdl.providers.audio import InternetArchive, SoundCloud, YouTube

    mocker.patch("spotdl.download.downloader.shutil.which", return_value="ffmpeg")
    youtube = RequestLimiter(min_interval=0, max_interval=0)
    archive = RequestLimiter(min_interval=0, max_interval=0)
    youtube.defer(RuntimeError("HTTP Error 429"))
    downloader = Downloader(
        settings={
            "audio_providers": ["youtube", "internet-archive", "soundcloud"],
            "lyrics_providers": [],
            "simple_tui": True,
        },
        loop=asyncio.get_running_loop(),
        request_limiter=youtube,
        provider_limiters={"internet-archive": archive},
    )
    mocker.patch(
        "spotdl.providers.audio.youtube.YoutubeDL",
        side_effect=lambda *_args, **_kwargs: pytest.fail(
            "A cooling-down YouTube service should not perform searches"
        ),
    )
    mocker.patch.object(
        InternetArchive,
        "search",
        return_value="https://archive.org/download/album/song.mp3",
    )
    soundcloud = mocker.patch.object(SoundCloud, "search")
    tracker = SongTracker(downloader.progress_handler, song())

    def download(provider, url, download=False):
        assert provider.request_limiter is archive
        with archive.slot(skip_cooldown=True):
            info = {"id": "song", "ext": "mp3"}
            Path(provider.audio_handler.prepare_filename(info)).write_bytes(b"audio")
            return info

    mocker.patch.object(AudioProvider, "get_download_metadata", download)
    try:
        info, url, audio = await downloader._download_audio(song(), tracker, tmp_path)
        assert info["id"] == "song"
        assert url.startswith("https://archive.org/")
        assert youtube.state[1] > 0
        soundcloud.assert_not_called()
        audio.audio_handler.close()
    finally:
        for provider in downloader.audio_providers:
            provider.audio_handler.close()


@pytest.mark.asyncio
async def test_failed_sources_are_all_reported_without_accepting_a_playlist(
    mocker, tmp_path
):
    from spotdl.providers.audio import InternetArchive, SoundCloud, YouTube

    mocker.patch("spotdl.download.downloader.get_temp_path", return_value=tmp_path)
    mocker.patch("spotdl.download.downloader.shutil.which", return_value="ffmpeg")
    for provider in (YouTube, InternetArchive, SoundCloud):
        mocker.patch.object(
            provider, "search", return_value="https://example.com/track"
        )
    mocker.patch.object(
        AudioProvider,
        "get_download_metadata",
        return_value={"entries": [{"id": "album"}]},
    )
    downloader = Downloader(
        settings={"lyrics_providers": [], "simple_tui": True},
        loop=asyncio.get_running_loop(),
    )
    try:
        _, path = await downloader.async_search_and_download(song())
        assert path is None
        assert len(downloader.errors) == 1
        assert all(
            source in downloader.errors[0]
            for source in ("YouTube", "InternetArchive", "SoundCloud")
        )
        assert "Expected one audio file" in downloader.errors[0]
        assert not any(path.is_dir() for path in tmp_path.iterdir())
    finally:
        for provider in downloader.audio_providers:
            provider.audio_handler.close()
