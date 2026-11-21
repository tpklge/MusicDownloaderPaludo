from pathlib import Path

import pytest

from spotdl.types.song import Song
from spotdl.utils.local_library import LocalLibrary, metadata_keys

TRACK_ID = "0kx3ml8bdAYrQtcIwvkhp8"


def audio_file(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"audio")
    return path


def song(**extra):
    return Song.from_missing_data(
        name="Song", artist="Artist", artists=["Artist"], **extra
    )


def test_recognizes_renamed_recording_by_spotify_id(monkeypatch, tmp_path):
    existing = audio_file(tmp_path, "old-playlist/renamed.mp3")
    monkeypatch.setattr(
        "spotdl.utils.local_library.get_file_metadata",
        lambda _: {"url": f"https://open.spotify.com/track/{TRACK_ID}?si=old"},
    )
    library = LocalLibrary()
    assert library.scan(tmp_path) == 1
    assert (
        library.find(song(url=f"spotify:track:{TRACK_ID}"), tmp_path / "new/Song.mp3")
        == existing
    )


def test_recognizes_recording_by_isrc_in_another_format(monkeypatch, tmp_path):
    existing = audio_file(tmp_path, "other-playlist/renamed.flac")
    monkeypatch.setattr(
        "spotdl.utils.local_library.get_file_metadata",
        lambda _: {"isrc": "US-ABC-12-34567"},
    )
    library = LocalLibrary()
    library.scan(tmp_path)
    assert (
        library.find(song(isrc="USABC1234567"), tmp_path / "new/Song.mp3") == existing
    )


def test_recognizes_exact_artist_title_tags(monkeypatch, tmp_path):
    existing = audio_file(tmp_path, "nested/deeper/renamed.m4a")
    monkeypatch.setattr(
        "spotdl.utils.local_library.get_file_metadata",
        lambda _: {"name": " SONG ", "artists": ["artist"]},
    )
    library = LocalLibrary()
    library.scan(tmp_path)
    assert library.find(song(), tmp_path / "new/Song.mp3") == existing


def test_filename_fallback_handles_bad_tags_case_and_unicode(monkeypatch, tmp_path):
    existing = audio_file(tmp_path, "old/Artist - Cafe\u0301.MP3")

    def bad_tags(_):
        raise ValueError("malformed tags")

    monkeypatch.setattr("spotdl.utils.local_library.get_file_metadata", bad_tags)
    library = LocalLibrary()
    library.scan(tmp_path)
    assert library.find(song(), tmp_path / "new/artist - café.mp3") == existing


def test_distinct_recordings_with_same_title_are_not_skipped(monkeypatch, tmp_path):
    audio_file(tmp_path, "old/Artist - Song.mp3")
    monkeypatch.setattr(
        "spotdl.utils.local_library.get_file_metadata",
        lambda _: {"name": "Song", "artists": ["Artist"], "isrc": "USABC1234567"},
    )
    library = LocalLibrary()
    library.scan(tmp_path)
    assert (
        library.find(song(isrc="USABC9876543"), tmp_path / "new/Artist - Song.mp3")
        is None
    )


def test_empty_partial_and_unrelated_files_are_not_matches(monkeypatch, tmp_path):
    audio_file(tmp_path, "old/Artist - Song.mp3.part")
    audio_file(tmp_path, "old/Artist - Other.mp3")
    (tmp_path / "empty").mkdir()
    (tmp_path / "empty/Artist - Song.mp3").touch()
    monkeypatch.setattr("spotdl.utils.local_library.get_file_metadata", lambda _: None)
    library = LocalLibrary()
    assert library.scan(tmp_path) == 1
    assert library.find(song(), tmp_path / "new/Artist - Song.mp3") is None


def test_deleted_files_are_rechecked(monkeypatch, tmp_path):
    existing = audio_file(tmp_path, "old/Artist - Song.mp3")
    monkeypatch.setattr("spotdl.utils.local_library.get_file_metadata", lambda _: None)
    library = LocalLibrary()
    library.scan(tmp_path)
    existing.unlink()
    assert library.find(song(), tmp_path / "new/Artist - Song.mp3") is None


@pytest.mark.parametrize(
    "url",
    [
        f"https://open.spotify.com/intl-pt/track/{TRACK_ID}?si=test",
        f"spotify:track:{TRACK_ID}",
        f"https://open.spotify.com/track/{TRACK_ID}",
    ],
)
def test_spotify_keys_are_canonical(url):
    assert ("spotify", TRACK_ID) in metadata_keys({"url": url})
