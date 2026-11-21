"""Archive search, track selection and malformed-response regression tests."""

import pytest
import requests

from spotdl.providers.audio.internet_archive import InternetArchive
from spotdl.types.song import Song


def metadata(files=None, **extra):
    return {
        "metadata": {"title": "Test Album", "creator": ["Test Artist"], **extra},
        "files": (
            files
            if files is not None
            else [
                {
                    "name": "01 Test Song.flac",
                    "title": "Test Song",
                    "length": "03:00",
                    "format": "Flac",
                    "size": "40000",
                },
                {
                    "name": "01 Test Song.mp3",
                    "original": "01 Test Song.flac",
                    "format": "VBR MP3",
                    "size": "20000",
                },
                {"name": "02 Other.mp3", "title": "Other", "length": "200"},
            ]
        ),
    }


def install_requests(mocker, payloads):
    responses = []
    for payload in payloads:
        response = mocker.Mock()
        response.json.return_value = payload
        responses.append(response)
    return mocker.patch(
        "spotdl.providers.audio.internet_archive.requests.get", side_effect=responses
    )


def test_search_selects_one_public_file_per_track_and_inherits_tags(mocker):
    get = install_requests(
        mocker, [{"response": {"docs": [{"identifier": "album"}]}}, metadata()]
    )
    results = InternetArchive().get_results("Test Artist - Test Song")
    assert len(results) == 2
    assert results[0].url == "https://archive.org/download/album/01%20Test%20Song.mp3"
    assert results[0].name == "Test Song"
    assert results[0].duration == 180
    assert results[0].artists == ("Test Artist",)
    assert (
        get.call_args_list[0].kwargs["params"]["q"]
        == 'mediatype:audio AND creator:"Test Artist" AND "Test Song"'
    )
    assert get.call_args_list[0].kwargs["timeout"] == 20
    assert get.call_args_list[1].args[0] == "https://archive.org/metadata/album"


def test_archive_match_uses_existing_artist_title_duration_filters(mocker):
    install_requests(
        mocker, [{"response": {"docs": [{"identifier": "album"}]}}, metadata()]
    )
    provider = InternetArchive()
    song = Song.from_missing_data(
        name="Test Song", artist="Test Artist", artists=["Test Artist"], duration=180
    )
    assert (
        provider.search(song)
        == "https://archive.org/download/album/01%20Test%20Song.mp3"
    )


@pytest.mark.parametrize(
    "flag", ["access-restricted-item", "stream_only", "nodownload", "is_dark"]
)
def test_restricted_items_are_not_candidates(flag):
    provider = InternetArchive()
    assert provider._file_results("album", metadata(**{flag: "true"}), "query") == []


def test_private_empty_non_audio_and_traversal_files_are_excluded():
    provider = InternetArchive()
    payload = metadata(
        files=[
            {"name": "private.mp3", "private": "true"},
            {"name": "empty.mp3", "size": "0"},
            {"name": "cover.jpg"},
            {"name": "../invalid.mp3"},
            {"name": "/invalid.mp3"},
            None,
            {},
            {"name": "valid.wav", "title": "Test Song", "length": "180"},
        ]
    )
    results = provider._file_results("album", payload, "query")
    assert len(results) == 1
    assert results[0].url.endswith("/valid.wav")


@pytest.mark.parametrize(
    "payload", [None, {}, {"response": []}, {"response": {"docs": None}}]
)
def test_malformed_search_response_returns_no_results(mocker, payload):
    install_requests(mocker, [payload])
    assert InternetArchive().get_results("artist - title") == []


def test_metadata_lookup_is_bounded_and_duplicate_documents_do_not_duplicate_results(
    mocker,
):
    documents = [{"identifier": "album"}] * 8
    get = install_requests(
        mocker, [{"response": {"docs": documents}}, *[metadata()] * 5]
    )
    results = InternetArchive().get_results("artist - title")
    assert get.call_count == 6
    assert len(results) == 2


def test_transient_item_error_does_not_hide_other_results(mocker):
    provider = InternetArchive()
    mocker.patch.object(
        provider,
        "_get_json",
        side_effect=[
            {"response": {"docs": [{"identifier": "bad"}, {"identifier": "good"}]}},
            requests.ConnectionError("offline"),
            metadata(),
        ],
    )
    assert len(provider.get_results("artist - title")) == 2


def test_unknown_duration_does_not_assume_album_length():
    provider = InternetArchive()
    results = provider._file_results(
        "album", metadata(files=[{"name": "song.mp3", "length": "nan"}]), "query"
    )
    assert results[0].duration == 0


def test_artist_search_falls_back_to_full_text_when_artist_field_has_no_results(mocker):
    get = install_requests(
        mocker,
        [
            {"response": {"docs": []}},
            {"response": {"docs": [{"identifier": "album"}]}},
            metadata(),
        ],
    )
    assert InternetArchive().get_results("Test Artist - Test Song")
    assert 'creator:"Test Artist"' in get.call_args_list[0].kwargs["params"]["q"]
    assert (
        get.call_args_list[1].kwargs["params"]["q"]
        == 'mediatype:audio AND ("Test Artist" AND "Test Song")'
    )
