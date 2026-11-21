"""Index downloaded audio locally without issuing search or download requests."""

import logging
import re
import threading
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from spotdl.types.song import Song
from spotdl.utils.metadata import get_file_metadata

logger = logging.getLogger(__name__)
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".opus", ".ogg", ".wav"}
Identity = Tuple[str, str]


def normalized_name(value: str) -> str:
    """Compare case and equivalent Unicode while preserving version suffixes."""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def nonempty_file(path: Path) -> bool:
    """Ignore disappeared, inaccessible, empty and incomplete download files."""
    try:
        return (
            path.suffix.lower() in AUDIO_EXTENSIONS
            and path.is_file()
            and path.stat().st_size > 0
        )
    except OSError:
        return False


def metadata_keys(metadata: Dict[str, Any]) -> List[Identity]:
    """Prefer recording IDs, then exact artist/title matches over filenames."""
    keys: List[Identity] = []
    url = str(metadata.get("url") or "").strip()
    track_id = str(metadata.get("song_id") or "")
    if url:
        parsed = urlsplit(url)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.hostname == "open.spotify.com"
            and len(parts) >= 2
            and parts[-2] == "track"
        ):
            track_id = parts[-1]
        elif url.startswith("spotify:track:"):
            track_id = url.rsplit(":", 1)[-1]
        else:
            keys.append(("url", url.split("?", 1)[0].rstrip("/")))
    if track_id:
        keys.insert(0, ("spotify", track_id))
    isrc = re.sub(r"[\s-]", "", str(metadata.get("isrc") or "")).upper()
    if isrc:
        keys.append(("isrc", isrc))
    artists = metadata.get("artists") or metadata.get("artist") or []
    if isinstance(artists, str):
        artists = [artists]
    title = normalized_name(str(metadata.get("name") or ""))
    names = sorted({normalized_name(str(artist)) for artist in artists if artist})
    if title and names:
        keys.append(("title_artists", "\0".join([title, *names])))
    return keys


def song_keys(song: Song) -> List[Identity]:
    """Extract only identity fields rather than copying artwork or full metadata."""
    return metadata_keys(
        {
            key: getattr(song, key, None)
            for key in ("url", "song_id", "isrc", "name", "artists", "artist")
        }
    )


@dataclass(frozen=True)
class LocalAudio:
    """A local file and the identity read from its tags."""

    path: Path
    keys: Tuple[Identity, ...]


class LocalLibrary:
    """Share an audio index between the preflight scan and download workers."""

    def __init__(self) -> None:
        self._index: Dict[Identity, List[LocalAudio]] = defaultdict(list)
        self._guard = threading.Lock()

    def register(self, path: Path, keys: List[Identity]) -> None:
        """Publish a finished download before another worker searches for it."""
        if not nonempty_file(path):
            return
        entry = LocalAudio(path, tuple(keys))
        with self._guard:
            for key in [*keys, ("filename", normalized_name(path.stem))]:
                if entry not in self._index[key]:
                    self._index[key].append(entry)

    def scan(self, root: Path, on_file: Optional[Callable[[int], None]] = None) -> int:
        """Scan the output tree once, tolerating files with missing/bad tags."""
        count = 0
        for path in root.rglob("*"):
            if not nonempty_file(path):
                continue
            try:
                metadata = get_file_metadata(path) or {}
                keys = metadata_keys(metadata)
            except Exception as error:  # A bad tag must not abort a whole library scan.
                logger.debug("Could not read local audio tags for %s: %s", path, error)
                keys = []
            self.register(path, keys)
            count += 1
            if on_file:
                on_file(count)
        return count

    def find(self, song: Song, target: Path) -> Optional[Path]:
        """Find an existing recording anywhere under the scanned output root."""
        if nonempty_file(target):
            return target
        keys = song_keys(song)
        current = dict(keys)
        with self._guard:
            for key in [*keys, ("filename", normalized_name(target.stem))]:
                for entry in self._index.get(key, []):
                    # Distinct ISRCs identify different recordings even when
                    # filenames or artist/title tags happen to be identical.
                    recorded = dict(entry.keys)
                    if (
                        key[0] in ("filename", "title_artists")
                        and current.get("isrc")
                        and recorded.get("isrc")
                        and current["isrc"] != recorded["isrc"]
                    ):
                        continue
                    if nonempty_file(entry.path):
                        return entry.path
        return None
