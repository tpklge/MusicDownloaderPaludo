"""Search Internet Archive audio items and match individual public audio files."""

import logging
import math
import re
from contextlib import nullcontext
from pathlib import PurePosixPath
from typing import Any, Dict, List
from urllib.parse import quote

import requests

from spotdl.providers.audio.base import AudioProvider
from spotdl.types.result import Result
from spotdl.utils.config import GlobalConfig
from spotdl.utils.request_limiter import is_rate_limit_error

__all__ = ["InternetArchive"]
logger = logging.getLogger(__name__)
AUDIO_EXTENSIONS = ("mp3", "flac", "ogg", "opus", "m4a", "wav")


def _text(value: Any) -> str:
    """Archive metadata fields may be scalar values or lists."""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    return str(value or "")


def _duration(value: Any) -> float:
    """Read seconds or a colon-separated duration without guessing track length."""
    try:
        seconds = 0.0
        for part in str(value).split(":"):
            seconds = seconds * 60 + float(part)
        return seconds if math.isfinite(seconds) and seconds > 0 else 0
    except (ValueError, TypeError):
        return 0


def _enabled(value: Any) -> bool:
    """Handle JSON booleans as well as Archive's textual metadata flags."""
    return str(value).lower() in ("true", "1", "yes")


class InternetArchive(AudioProvider):
    """Use Archive search/metadata APIs; download one matched file via yt-dlp."""

    SUPPORTS_ISRC = False
    GET_RESULTS_OPTS: List[Dict[str, Any]] = [{}]
    MAX_ITEMS = 5

    def _get_json(self, url: str, **params: Any) -> Dict[str, Any]:
        """Pace each API request, including per-item metadata lookups."""
        with (
            self.request_limiter.slot(skip_cooldown=True)
            if self.request_limiter
            else nullcontext()
        ):
            response = requests.get(
                url,
                params=params,
                timeout=20,
                proxies=GlobalConfig.get_parameter("proxies"),
                headers={"User-Agent": "spotDL (Internet Archive audio search)"},
            )
            response.raise_for_status()
            payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def get_results(self, search_term: str, **_kwargs: Any) -> List[Result]:
        """Inspect a bounded number of items, never returning whole albums."""
        phrases = search_term.split(" - ", 1)
        literals = [
            '"' + phrase.replace("\\", "\\\\").replace('"', '\\"') + '"'
            for phrase in phrases
            if phrase.strip()
        ]
        if not literals:
            return []
        queries = ["mediatype:audio AND (" + " AND ".join(literals) + ")"]
        if len(literals) == 2:
            # Avoid selecting podcasts which only credit a song in the description.
            queries.insert(
                0, f"mediatype:audio AND creator:{literals[0]} AND {literals[1]}"
            )
        documents: List[Any] = []
        for query in queries:
            search = self._get_json(
                "https://archive.org/advancedsearch.php",
                q=query,
                **{"fl[]": ["identifier"], "rows": self.MAX_ITEMS, "output": "json"},
            )
            response = search.get("response")
            rows = response.get("docs") if isinstance(response, dict) else None
            if not isinstance(rows, list):
                return []
            documents = rows
            if documents:
                break
        results: Dict[str, Result] = {}
        for document in documents[: self.MAX_ITEMS]:
            identifier = (
                document.get("identifier") if isinstance(document, dict) else None
            )
            if not isinstance(identifier, str) or not re.fullmatch(
                r"[\w.-]+", identifier
            ):
                continue
            try:
                payload = self._get_json(
                    f"https://archive.org/metadata/{quote(identifier, safe='')}"
                )
            except requests.RequestException as error:
                if is_rate_limit_error(error):
                    raise
                logger.debug(
                    "Archive metadata unavailable for %s: %s", identifier, error
                )
                continue
            for result in self._file_results(identifier, payload, search_term):
                results[result.url] = result
        return list(results.values())

    def _file_results(
        self, identifier: str, payload: Dict, search_term: str
    ) -> List[Result]:
        """Merge derivative tags with original-file tags and select one format."""
        metadata = payload.get("metadata")
        files = payload.get("files")
        if not isinstance(metadata, dict) or not isinstance(files, list):
            return []
        if any(
            _enabled(value)
            for value in (
                payload.get("is_dark"),
                metadata.get("is_dark"),
                metadata.get("access-restricted-item"),
                metadata.get("stream_only"),
                metadata.get("nodownload"),
            )
        ):
            return []
        by_name = {
            item["name"]: item
            for item in files
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        groups: Dict[str, List[Dict]] = {}
        for filename, item in by_name.items():
            path = PurePosixPath(filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.suffix.lower()[1:] not in AUDIO_EXTENSIONS
            ):
                continue
            if _enabled(item.get("private")) or str(item.get("size")) == "0":
                continue
            original = item.get("original")
            group = original if isinstance(original, str) else filename
            groups.setdefault(group, []).append(item)

        preferred = (self.output_format, *AUDIO_EXTENSIONS)
        results = []
        for original, variants in groups.items():
            item = min(
                variants,
                key=lambda row: preferred.index(
                    PurePosixPath(row["name"]).suffix.lower()[1:]
                ),
            )
            inherited = by_name.get(original, {})
            filename = item["name"]
            title = _text(item.get("title") or inherited.get("title"))
            if not title:
                title = _text(metadata.get("title")) if len(groups) == 1 else ""
                title = title or re.sub(
                    r"^\d+[\s._-]+", "", PurePosixPath(filename).stem
                )
            creator = (
                item.get("creator")
                or inherited.get("creator")
                or metadata.get("creator")
            )
            artists = (
                tuple(str(artist) for artist in creator if artist)
                if isinstance(creator, list)
                else (_text(creator),)
            )
            results.append(
                Result(
                    source="internet-archive",
                    verified=False,
                    name=title,
                    url=(
                        f"https://archive.org/download/{quote(identifier, safe='')}/"
                        f"{quote(filename, safe='/')}"
                    ),
                    duration=_duration(
                        item.get("length")
                        or inherited.get("length")
                        or item.get("duration")
                    ),
                    author=_text(creator),
                    artists=artists,
                    result_id=f"{identifier}/{filename}",
                    search_query=search_term,
                    views=0,
                    album=_text(
                        item.get("album")
                        or inherited.get("album")
                        or metadata.get("title")
                    ),
                )
            )
        return results
