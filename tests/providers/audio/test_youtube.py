from spotdl.providers.audio.youtube import YouTube
from spotdl.types.song import Song


def test_yt_search(mocker):
    mocker.patch(
        "spotdl.providers.audio.youtube.YoutubeDL.extract_info",
        return_value={
            "entries": [
                {
                    "id": "test_video_id",
                    "title": "Abstrakt - Nobody Else",
                    "duration": 162,
                    "uploader": "Abstrakt",
                    "view_count": 123456,
                }
            ]
        },
    )

    provider = YouTube()

    assert (
        provider.search(
            Song.from_dict(
                {
                    "name": "Nobody Else",
                    "artists": ["Abstrakt"],
                    "artist": "Abstrakt",
                    "album_id": "0kx3ml8bdAYrQtcIwvkhp8",
                    "album_name": "Nobody Else",
                    "album_artist": "Abstrakt",
                    "album_type": "single",
                    "genres": [],
                    "disc_number": 1,
                    "disc_count": 1,
                    "duration": 162.406,
                    "year": 2022,
                    "date": "2022-03-17",
                    "track_number": 1,
                    "tracks_count": 1,
                    "isrc": "GB2LD2210007",
                    "song_id": "0kx3ml8bdAYrQtcIwvkhp8",
                    "cover_url": "https://i.scdn.co/image/ab67616d0000b27345f5ba253b9825efc88bc236",
                    "explicit": False,
                    "publisher": "NCS",
                    "url": "https://open.spotify.com/track/0kx3ml8bdAYrQtcIwvkhp8",
                    "copyright_text": "2022 NCS",
                    "download_url": None,
                }
            )
        )
        is not None
    )


def test_yt_get_results(mocker):
    mocker.patch(
        "spotdl.providers.audio.youtube.YoutubeDL.extract_info",
        return_value={
            "entries": [
                {
                    "id": f"video_{index}",
                    "title": f"Lost Identities Moments {index}",
                    "duration": 180 + index,
                    "uploader": "Lost Identities",
                    "view_count": 1000 + index,
                }
                for index in range(6)
            ]
        },
    )

    provider = YouTube()

    results = provider.get_results("Lost Identities Moments")

    assert results and len(results) > 5
    assert results[0].url == "https://www.youtube.com/watch?v=video_0"
    assert results[0].duration == 180
    assert results[0].author == "Lost Identities"


def test_yt_get_results_normalizes_null_metadata(mocker):
    mocker.patch(
        "spotdl.providers.audio.youtube.YoutubeDL.extract_info",
        return_value={
            "entries": [
                {
                    "id": "video_0",
                    "title": "Live Stream",
                    "duration": None,
                    "uploader": None,
                    "view_count": None,
                }
            ]
        },
    )

    provider = YouTube()

    results = provider.get_results("live stream")

    assert len(results) == 1
    assert results[0].duration == 0
    assert results[0].author == ""
    assert results[0].views == 0


def test_youtube_search_uses_flat_results_and_does_not_extract_each_video(mocker):
    youtube_dl = mocker.patch("spotdl.providers.audio.youtube.YoutubeDL")
    youtube_dl.return_value.__enter__.return_value.extract_info.return_value = {
        "entries": []
    }
    provider = YouTube()
    provider.get_results("artist song")
    options = youtube_dl.call_args.args[0]
    assert options["extract_flat"] == "in_playlist"
    assert options["skip_download"] is True
    youtube_dl.return_value.__enter__.return_value.extract_info.assert_called_once_with(
        "ytsearch10:artist song", download=False
    )


def test_zero_views_do_not_generate_additional_metadata_requests(mocker):
    from spotdl.types.result import Result

    provider = YouTube()
    get_views = mocker.patch.object(provider, "get_views")
    best = Result(
        source="YouTube",
        url="best",
        verified=False,
        name="Song",
        duration=180,
        author="Artist",
        result_id="best",
        views=0,
    )
    other = Result(
        source="YouTube",
        url="other",
        verified=False,
        name="Song",
        duration=180,
        author="Artist",
        result_id="other",
        views=0,
    )
    result, score = provider.get_best_result({best: 90, other: 85})
    assert result is best
    assert score == 90
    get_views.assert_not_called()


def test_paced_matching_does_not_fetch_missing_view_counts(mocker):
    from spotdl.types.result import Result
    from spotdl.utils.request_limiter import RequestLimiter

    provider = YouTube()
    provider.request_limiter = RequestLimiter()
    get_views = mocker.patch.object(provider, "get_views")
    results = {
        Result(
            source="YouTube",
            url=f"video{index}",
            verified=False,
            name="Song",
            duration=180,
            author="Artist",
            result_id=str(index),
        ): score
        for index, score in enumerate((90, 85))
    }
    _, score = provider.get_best_result(results)
    assert score == 90
    get_views.assert_not_called()
