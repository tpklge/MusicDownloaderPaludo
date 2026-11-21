"""SoundCloud search uses flat metadata and initializes no separate API client."""

import pytest

from spotdl.providers.audio.soundcloud import SoundCloud


def entry(**extra):
    return {
        "id": "123",
        "title": "Test Song",
        "duration": 180,
        "webpage_url": "https://soundcloud.com/artist/test-song",
        "uploader": "Label",
        "artists": ["Test Artist"],
        **extra,
    }


def search_handler(mocker, entries):
    handler = mocker.patch("spotdl.providers.audio.soundcloud.YoutubeDL")
    handler.return_value.__enter__.return_value.extract_info.return_value = {
        "entries": entries
    }
    return handler


def test_soundcloud_does_not_fetch_anything_during_initialization(mocker):
    from yt_dlp import YoutubeDL

    extract = mocker.patch.object(YoutubeDL, "extract_info")
    SoundCloud()
    extract.assert_not_called()


def test_soundcloud_flat_search_uses_real_artist_and_seconds(mocker):
    handler = search_handler(mocker, [entry()])
    result = SoundCloud().get_results("Test Artist - Test Song")[0]
    assert result.duration == 180
    assert result.author == "Test Artist"
    assert result.artists == ("Test Artist",)
    assert handler.call_args.args[0]["extract_flat"] == "in_playlist"
    handler.return_value.__enter__.return_value.extract_info.assert_called_once_with(
        "scsearch20:Test Artist - Test Song",
        download=False,
    )


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        entry(is_preview=True),
        entry(availability="premium_only"),
        entry(webpage_url="https://example.com/song"),
    ],
)
def test_soundcloud_ignores_unusable_results(mocker, row):
    search_handler(mocker, [row])
    assert SoundCloud().get_results("query") == []


def test_soundcloud_deduplicates_and_handles_null_metadata(mocker):
    search_handler(
        mocker,
        [
            entry(artists=None, duration=None, uploader=None),
            entry(artists=None, duration=None, uploader=None),
        ],
    )
    results = SoundCloud().get_results("query")
    assert len(results) == 1
    assert results[0].duration == 0
    assert results[0].views == 0
    assert results[0].author == ""
