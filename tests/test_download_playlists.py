from types import SimpleNamespace

import pytest
from rich.progress import Progress

from scripts import download_playlists
from spotdl.types.album import Album
from spotdl.types.playlist import Playlist
from spotdl.types.song import Song

PLAYLIST_ID = "6RIZc5hbnePqSXayV9k3Dx"
ALBUM_ID = "5devPxQnSKVF2Ed0CVwQZh"


@pytest.mark.parametrize(
    "url, kind, spotify_id",
    [
        (f"https://open.spotify.com/playlist/{PLAYLIST_ID}", "playlist", PLAYLIST_ID),
        (
            f"https\\://open.spotify.com/playlist/{PLAYLIST_ID}?si=example",
            "playlist",
            PLAYLIST_ID,
        ),
        (
            f"https\\://open.spotify.com/intl-pt/album/{ALBUM_ID}?si=example",
            "album",
            ALBUM_ID,
        ),
    ],
)
def test_load_one_playlist_routes_and_normalizes_urls(
    monkeypatch, url, kind, spotify_id
):
    calls = []
    song = SimpleNamespace()

    def metadata_loader(expected_kind):
        def load(canonical_url):
            calls.append((expected_kind, canonical_url))
            return {"name": "Minha coleção"}, [song]

        return staticmethod(load)

    monkeypatch.setattr(Album, "get_metadata", metadata_loader("album"))
    monkeypatch.setattr(Playlist, "get_metadata", metadata_loader("playlist"))

    result = download_playlists.load_one_playlist(url)

    canonical_url = f"https://open.spotify.com/{kind}/{spotify_id}"
    assert calls == [(kind, canonical_url)]
    assert result == {
        "name": "Minha coleção",
        "url": canonical_url,
        "songs": [song],
    }
    assert song.list_name == "Minha coleção"
    assert song.list_url == canonical_url


@pytest.mark.parametrize(
    "url",
    [
        "",
        f"https://example.com/playlist/{PLAYLIST_ID}",
        f"https://open.spotify.com/track/{PLAYLIST_ID}",
        "https://open.spotify.com/playlist/invalid",
        "https://open.spotify.com/intl-pt",
    ],
)
def test_normalize_spotify_url_rejects_unsupported_input(url):
    with pytest.raises(ValueError, match="URL inválida"):
        download_playlists.normalize_spotify_url(url)


def test_load_playlists_preserves_input_order_and_cleans_progress(monkeypatch):
    urls = [
        f"https://open.spotify.com/playlist/{PLAYLIST_ID}",
        f"https://open.spotify.com/album/{ALBUM_ID}",
    ]
    monkeypatch.setattr(
        download_playlists, "load_one_playlist", lambda url: {"url": url}
    )
    progress = Progress(disable=True)

    result = download_playlists.load_playlists(urls, progress)

    assert result == [{"url": url} for url in urls]
    assert progress.tasks == []


def test_load_playlists_reports_failing_url_and_cleans_progress(monkeypatch):
    url = f"https://open.spotify.com/playlist/{PLAYLIST_ID}"

    def fail(_url):
        raise KeyError("ownerV2")

    monkeypatch.setattr(download_playlists, "load_one_playlist", fail)
    progress = Progress(disable=True)

    with pytest.raises(RuntimeError, match=url) as error:
        download_playlists.load_playlists([url], progress)

    assert isinstance(error.value.__cause__, KeyError)
    assert "ownerV2" in str(error.value)
    assert progress.tasks == []


def make_playlists(names=("Coleção",), songs_per_playlist=1):
    return [
        {
            "name": name,
            "songs": [
                Song.from_missing_data(
                    name=f"Song {index}-{number}",
                    artist="Artist",
                    artists=["Artist"],
                    url=f"spotify:{index}:{number}",
                    list_name=name,
                )
                for number in range(songs_per_playlist)
            ],
        }
        for index, name in enumerate(names)
    ]


def run_jobs(
    monkeypatch, tmp_path, download, playlists=None, workers=1, delay=0, retries=4
):
    monkeypatch.setattr(download_playlists, "download_one", download)
    progress = Progress(disable=True)
    summary = download_playlists.run_downloads(
        playlists or make_playlists(),
        tmp_path,
        workers,
        progress,
        download_playlists.DownloadRuntime(),
        download_playlists.RetryPolicy(retries=retries, delay=delay),
    )
    return summary, progress


def test_retries_four_times_before_success(monkeypatch, tmp_path):
    attempts = []

    def download(song, output, runtime, report, attempt):
        attempts.append(attempt)
        report(55, "Downloading")
        if attempt < 5:
            raise OSError("temporary failure")
        return download_playlists.DownloadResult(path=tmp_path / "song.mp3")

    summary, progress = run_jobs(monkeypatch, tmp_path, download)

    assert attempts == [1, 2, 3, 4, 5]
    assert summary.downloaded == 1
    assert summary.failed == 0
    assert progress.tasks[0].completed == 1
    assert summary.errors == []


@pytest.mark.parametrize("retries", [4, 6])
def test_missing_result_is_retried_and_reported_as_failure(
    monkeypatch, tmp_path, retries
):
    attempts = []

    def download(song, output, runtime, report, attempt):
        attempts.append(attempt)
        return download_playlists.DownloadResult(error="yt-dlp failed")

    summary, progress = run_jobs(monkeypatch, tmp_path, download, retries=retries)

    assert attempts == list(range(1, retries + 2))
    assert summary.failed == 1
    assert summary.downloaded == 0
    assert summary.errors[0]["attempts"] == retries + 1
    assert summary.errors[0]["error"] == "yt-dlp failed"
    assert "0 baixadas" in progress.tasks[0].fields["status"]
    assert "1 falhas" in progress.tasks[0].fields["status"]


@pytest.mark.parametrize("other_song_duration", [0, 30])
def test_retry_wait_releases_worker_for_other_songs(
    monkeypatch, tmp_path, other_song_duration
):
    now = [0.0]
    monkeypatch.setattr(
        download_playlists,
        "time",
        SimpleNamespace(
            monotonic=lambda: now[0],
            sleep=lambda seconds: now.__setitem__(0, now[0] + seconds),
        ),
    )
    calls = []

    def download(song, output, runtime, report, attempt):
        calls.append((song.name, attempt, now[0]))
        if song.name == "Song 0-0" and attempt == 1:
            raise OSError("temporary")
        if song.name == "Song 0-1":
            now[0] += other_song_duration
        return download_playlists.DownloadResult(path=tmp_path / song.name)

    summary, _ = run_jobs(
        monkeypatch,
        tmp_path,
        download,
        playlists=make_playlists(songs_per_playlist=3),
        delay=15,
    )

    assert [(name, attempt) for name, attempt, _ in calls] == [
        ("Song 0-0", 1),
        ("Song 0-1", 1),
        ("Song 0-2", 1),
        ("Song 0-0", 2),
    ]
    assert calls[1][2] == 0
    assert calls[2][2] == other_song_duration
    assert calls[3][2] >= 15
    assert summary.downloaded == 3


def test_same_playlist_names_have_separate_progress(monkeypatch, tmp_path):
    def download(song, output, runtime, report, attempt):
        return download_playlists.DownloadResult(
            path=tmp_path / song.name, skipped=True
        )

    summary, progress = run_jobs(
        monkeypatch,
        tmp_path,
        download,
        playlists=make_playlists(("Same", "Same")),
        workers=12,
    )

    assert summary.skipped == 2
    assert progress.tasks[2].completed == 1
    assert progress.tasks[3].completed == 1


def test_retry_backoff_grows_and_is_capped(monkeypatch):
    monkeypatch.setattr(download_playlists.random, "uniform", lambda *_: 1)
    policy = download_playlists.RetryPolicy()
    assert [policy.wait(i) for i in range(1, 8)] == [15, 30, 60, 120, 240, 300, 300]


def test_download_one_detects_swallowed_errors_and_removes_partial_file(
    monkeypatch, tmp_path
):
    import asyncio

    from spotdl.download.downloader import Downloader

    target = tmp_path / "partial.mp3"
    monkeypatch.setattr("spotdl.utils.formatter.create_file_name", lambda *_: target)
    loop = asyncio.new_event_loop()
    settings_seen = []

    def init(self, settings, request_limiter, provider_limiters):
        settings_seen.append(settings)
        self.loop = loop
        self.errors = ["YT-DLP download error: HTTP Error 429"]
        self.progress_handler = SimpleNamespace(update_callback=None)

    def fail(self, song):
        target.write_bytes(b"incomplete")
        return song, None

    monkeypatch.setattr(Downloader, "__init__", init)
    monkeypatch.setattr(Downloader, "download_song", fail)
    song = make_playlists()[0]["songs"][0]

    with pytest.raises(RuntimeError, match="429"):
        download_playlists.download_one(
            song,
            tmp_path,
            download_playlists.DownloadRuntime(),
            lambda *_: None,
            attempt=2,
        )

    assert not target.exists()
    assert loop.is_closed()
    assert settings_seen[0]["audio_providers"] == [
        "youtube",
        "internet-archive",
        "soundcloud",
    ]
    assert settings_seen[0]["save_errors"] is None


def test_existing_song_skips_network(monkeypatch, tmp_path):
    target = tmp_path / "existing.mp3"
    target.write_bytes(b"existing audio")
    monkeypatch.setattr("spotdl.utils.formatter.create_file_name", lambda *_: target)
    monkeypatch.setattr(
        "spotdl.download.downloader.Downloader",
        lambda **_: pytest.fail(
            "Existing files should not initialize network providers"
        ),
    )
    result = download_playlists.download_one(
        make_playlists()[0]["songs"][0],
        tmp_path,
        download_playlists.DownloadRuntime(),
        lambda *_: None,
    )
    assert result.skipped
    assert result.path == target


def test_cli_returns_failure_and_writes_report(monkeypatch, tmp_path):
    import json

    monkeypatch.setattr(
        "sys.argv", ["download_playlists.py", "url", "--output", str(tmp_path)]
    )
    monkeypatch.setattr(
        download_playlists, "load_playlists", lambda *_: make_playlists()
    )
    monkeypatch.setattr(download_playlists.RetryPolicy, "wait", lambda *_: 0)
    monkeypatch.setattr(
        download_playlists,
        "download_one",
        lambda *_args, **_kwargs: download_playlists.DownloadResult(
            error="unavailable"
        ),
    )

    assert download_playlists.main() == 1
    report = json.loads((tmp_path / "download-errors.json").read_text())
    assert report[0]["attempts"] == 5
    assert report[0]["error"] == "unavailable"


@pytest.mark.parametrize("value", ["nan", "inf", "-1"])
def test_cli_rejects_invalid_delays(value):
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        download_playlists.finite_nonnegative(value)


def test_preflight_skips_complete_playlist_without_starting_downloads(
    monkeypatch, tmp_path
):
    playlists = make_playlists(songs_per_playlist=2)
    for song in playlists[0]["songs"]:
        path, _ = download_playlists.song_output(song, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"existing audio")
    monkeypatch.setattr("spotdl.utils.local_library.get_file_metadata", lambda _: None)

    def should_not_download(*_args, **_kwargs):
        pytest.fail("Preflight should remove existing songs from the queue")

    summary, progress = run_jobs(
        monkeypatch, tmp_path, should_not_download, playlists=playlists
    )
    assert summary.skipped == 2
    assert summary.downloaded == 0
    assert summary.failed == 0
    assert progress.tasks[0].completed == 2
    assert not any("W1" in task.description for task in progress.tasks)


def test_existing_folder_only_downloads_missing_songs(monkeypatch, tmp_path):
    playlists = make_playlists(songs_per_playlist=2)
    first, _ = download_playlists.song_output(playlists[0]["songs"][0], tmp_path)
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"existing audio")
    (first.parent / "unrelated.mp3").write_bytes(b"unrelated")
    monkeypatch.setattr("spotdl.utils.local_library.get_file_metadata", lambda _: None)
    requested = []

    def download(song, output, runtime, report, attempt):
        requested.append(song.name)
        return download_playlists.DownloadResult(path=tmp_path / "new.mp3")

    summary, _ = run_jobs(monkeypatch, tmp_path, download, playlists=playlists)
    assert requested == ["Song 0-1"]
    assert summary.skipped == 1
    assert summary.downloaded == 1


def test_concurrent_same_song_in_different_playlists_downloads_only_once(
    monkeypatch, tmp_path
):
    import asyncio
    import concurrent.futures
    import threading

    from spotdl.download.downloader import Downloader

    first = make_playlists()[0]["songs"][0]
    second = Song.from_dict({**first.json, "list_name": "Outra playlist"})
    calls = []
    started = threading.Event()
    release = threading.Event()

    def init(self, settings, request_limiter, provider_limiters):
        self.loop = asyncio.new_event_loop()
        self.progress_handler = SimpleNamespace(update_callback=None)
        self.errors = []

    def download(self, song):
        calls.append(song.name)
        started.set()
        assert release.wait(timeout=3)
        path, _ = download_playlists.song_output(song, tmp_path)
        path.write_bytes(b"complete audio")
        return song, path

    monkeypatch.setattr(Downloader, "__init__", init)
    monkeypatch.setattr(Downloader, "download_song", download)
    runtime = download_playlists.DownloadRuntime()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first_job = executor.submit(
            download_playlists.download_one, first, tmp_path, runtime, lambda *_: None
        )
        try:
            assert started.wait(timeout=3)
            second_job = executor.submit(
                download_playlists.download_one,
                second,
                tmp_path,
                runtime,
                lambda *_: None,
            )
        finally:
            release.set()
        results = [first_job.result(timeout=3), second_job.result(timeout=3)]

    assert calls == [first.name]
    assert [result.skipped for result in results] == [False, True]
    assert results[0].path == results[1].path
    assert not (tmp_path / "Outra-playlist").exists()


def test_worker_progress_events_are_coalesced_and_errors_do_not_print_during_downloads(
    monkeypatch, tmp_path
):
    progress = Progress(disable=True)
    print_message = []
    monkeypatch.setattr(
        progress.console, "print", lambda *args, **kwargs: print_message.append(args)
    )

    def download(song, output, runtime, report, attempt):
        for value in range(1000):
            report(40 + value / 100, "Downloading: YouTube")
        return download_playlists.DownloadResult(error="temporary failure")

    monkeypatch.setattr(download_playlists, "download_one", download)
    summary = download_playlists.run_downloads(
        make_playlists(),
        tmp_path,
        1,
        progress,
        download_playlists.DownloadRuntime(),
        download_playlists.RetryPolicy(delay=0),
    )
    assert summary.failed == 1
    assert summary.errors[0]["error"] == "temporary failure"
    assert print_message == []


def test_progress_text_columns_keep_the_same_width_for_different_worker_names():
    from io import StringIO

    from rich.console import Console

    console = Console(file=StringIO(), width=120)
    progress = download_playlists.create_progress(console)
    first = progress.add_task("W1: Song", status="Buscando")
    widths = [column.get_table_column().width for column in progress.columns]
    progress.update(
        first,
        description="W1: " + "Música muito longa " * 10,
        status="Obtendo áudio: Internet Archive",
    )
    assert [column.get_table_column().width for column in progress.columns] == widths
    assert widths[0] is not None
    assert widths[3] is not None
