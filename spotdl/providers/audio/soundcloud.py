"""Search SoundCloud tracks using yt-dlp without extracting every candidate."""

from contextlib import nullcontext
from typing import Any, Dict, List
from urllib.parse import urlsplit

from yt_dlp import YoutubeDL

from spotdl.providers.audio.base import AudioProvider
from spotdl.types.result import Result

__all__ = ["SoundCloud"]


class SoundCloud(AudioProvider):
    """Search full tracks lazily, using publisher artist tags when available."""

    SUPPORTS_ISRC = False
    GET_RESULTS_OPTS: List[Dict[str, Any]] = [{}]

    def get_results(self, search_term: str, **_kwargs: Any) -> List[Result]:
        """Get at most twenty track candidates from a flat SoundCloud search."""
        options = {
            **self.audio_handler.params,
            "skip_download": True,
            "extract_flat": "in_playlist",
        }
        with (
            (
                self.request_limiter.slot(skip_cooldown=True)
                if self.request_limiter
                else nullcontext()
            ),
            YoutubeDL(options) as handler,
        ):
            payload = handler.extract_info(f"scsearch20:{search_term}", download=False)
        results: Dict[str, Result] = {}
        for entry in (payload or {}).get("entries") or []:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            url = entry.get("webpage_url") or entry.get("url")
            if not isinstance(url, str) or urlsplit(url).hostname not in (
                "soundcloud.com",
                "www.soundcloud.com",
                "api.soundcloud.com",
                "api-v2.soundcloud.com",
            ):
                continue
            if entry.get("availability") in (
                "private",
                "premium_only",
                "subscriber_only",
                "needs_auth",
            ):
                continue
            if (
                entry.get("is_preview")
                or "preview" in str(entry.get("format_id", "")).lower()
            ):
                continue
            artists = entry.get("artists") or [
                entry.get("artist") or entry.get("uploader") or ""
            ]
            if isinstance(artists, str):
                artists = [artists]
            results[url] = Result(
                source="soundcloud",
                url=url,
                name=entry.get("track") or entry.get("title") or "",
                verified=bool(entry.get("channel_is_verified")),
                duration=entry.get("duration") or 0,
                author=artists[0] if artists else "",
                artists=tuple(artists),
                result_id=str(entry["id"]),
                search_query=search_term,
                views=entry.get("view_count") or 0,
                album=entry.get("album"),
            )
        return list(results.values())
