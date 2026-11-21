"""Persistent local download queue for the graphical interface."""

import copy
import heapq
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable, Mapping, cast
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL

from spotdl.download.downloader import Downloader
from spotdl.types.album import Album
from spotdl.types.options import DownloaderOptions
from spotdl.types.playlist import Playlist
from spotdl.types.song import Song
from spotdl.utils.config import SPOTIFY_OPTIONS
from spotdl.utils.deno import get_local_deno_yt_dlp_options
from spotdl.utils.ffmpeg import get_ffmpeg_path
from spotdl.utils.formatter import create_file_name
from spotdl.utils.request_limiter import RequestLimiter
from spotdl.utils.spotify import SpotifyClient

logger = logging.getLogger(__name__)
TERMINAL = {"completed", "failed", "cancelled", "partial"}
SPOTIFY_LOCK = threading.Lock()


class Cancelled(Exception):
    """Stop a job at the next download callback or between items."""


def normalize_url(url: str, scope: str = "auto") -> tuple[str, str, str]:
    """Accept only supported public URLs and discard unrelated query arguments."""
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.port:
        raise ValueError("Cole um link válido do YouTube ou Spotify.")
    host = parsed.hostname
    parts = parsed.path.strip("/").split("/")
    query = parse_qs(parsed.query)
    if host == "open.spotify.com":
        if parts[0].startswith("intl-"):
            parts = parts[1:]
        if (
            len(parts) == 2
            and parts[0] in {"track", "album", "playlist"}
            and re.fullmatch(r"[A-Za-z0-9]{22}", parts[1])
        ):
            return "spotify", parts[0], f"https://open.spotify.com/{'/'.join(parts)}"
    if host in {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }:
        video = None
        if host in {"youtu.be", "www.youtu.be"}:
            video = parts[0]
        elif parts[0] == "watch":
            video = query.get("v", [None])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            video = parts[1]
        playlist = query.get("list", [None])[0]
        if playlist and (scope == "playlist" or (scope == "auto" and not video)):
            if re.fullmatch(r"[A-Za-z0-9_-]{2,150}", playlist):
                return (
                    "youtube",
                    "playlist",
                    f"https://www.youtube.com/playlist?list={playlist}",
                )
        elif (
            scope != "playlist" and video and re.fullmatch(r"[A-Za-z0-9_-]{11}", video)
        ):
            return "youtube", "video", f"https://www.youtube.com/watch?v={video}"
    raise ValueError(
        "Link não reconhecido. Use um vídeo, playlist ou álbum do YouTube/Spotify."
    )


def spotify_collection(url: str, kind: str) -> tuple[dict, list[Song]]:
    """Initialize Spotify only when it is actually requested."""
    with SPOTIFY_LOCK:
        if SpotifyClient._instance is None:  # pylint: disable=protected-access
            SpotifyClient.init(**SPOTIFY_OPTIONS)
    if kind == "track":
        song = Song.from_url(url)
        return {
            "name": song.name,
            "cover_url": song.cover_url,
            "author_name": song.artist,
        }, [song]
    collection = Playlist if kind == "playlist" else Album
    return collection.get_metadata(url)


def thumbnail(info: dict) -> str:
    """Choose a cover, falling back to the first playlist item."""
    candidates = [info, *(info.get("entries") or [])[:1]]
    for candidate in candidates:
        if not candidate:
            continue
        urls = [
            candidate.get("thumbnail"),
            *(item.get("url") for item in reversed(candidate.get("thumbnails") or [])),
        ]
        for url in urls:
            if url and urlsplit(url).scheme == "https":
                return url
        video_id = candidate.get("id", "")
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"
    return ""


class DownloadManager:
    """Run downloads outside the HTTP loop, keeping finished files and history."""

    def __init__(self, root: Path, settings: Mapping[str, Any], workers: int = 2):
        if not 1 <= workers <= 32:
            raise ValueError("Use entre 1 e 32 workers.")
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = dict(settings)
        self.history_path = self.root / ".interface-history.json"
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, threading.Event] = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media")
        self.item_executor = ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="song"
        )
        self.workers = workers
        self.active_workers: set[int] = set()
        self.item_events: dict[tuple[str, int], threading.Event] = {}
        self.closed = False
        self.retries = 4
        self.retry_delay = 10.0
        self.file_locks: dict[str, threading.Lock] = {}
        self.limiter = RequestLimiter()
        self.service_limiters = {
            "internet-archive": RequestLimiter(min_interval=1, max_interval=2),
            "soundcloud": RequestLimiter(min_interval=3, max_interval=5),
        }
        self._load()

    def configure(
        self,
        workers: int,
        retries: int = 4,
        retry_delay: float = 10,
        youtube_concurrency: int = 2,
        request_interval: float = 5,
        bitrate: str = "192k",
        cookie_file: str | None = None,
    ):
        """Resize the live worker budget; other choices apply to new jobs."""
        if (
            not 1 <= workers <= 32
            or not 0 <= retries <= 10
            or not 0 <= retry_delay <= 300
        ):
            raise ValueError("Use 1–32 workers, 0–10 repetições e espera de até 300s.")
        if not 1 <= youtube_concurrency <= 8 or not 0 <= request_interval <= 60:
            raise ValueError("Configuração de requisições inválida.")
        if bitrate not in {"128k", "192k", "256k", "320k"}:
            raise ValueError("Bitrate inválido.")
        with self.lock:
            self.workers, self.retries, self.retry_delay = workers, retries, retry_delay
            self.settings.update(bitrate=bitrate, cookie_file=cookie_file)
            self.limiter.configure(
                youtube_concurrency,
                request_interval,
                max(request_interval, request_interval * 2),
            )

    def _load(self):
        try:
            saved = json.loads(self.history_path.read_text(encoding="utf-8"))
            for job in saved:
                job.setdefault("items", [])
                for item in job["items"]:
                    if item["status"] not in TERMINAL:
                        item.update(
                            status="cancelled",
                            message="Interrompido ao fechar",
                            worker=None,
                        )
                if job["status"] not in TERMINAL:
                    job.update(
                        status="cancelled",
                        message="Interrompido ao fechar o programa. Tente novamente.",
                    )
                self.jobs[job["id"]] = job
        except (OSError, ValueError, KeyError, TypeError):
            self.jobs = {}

    def _save(self):
        temporary = self.history_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(list(self.jobs.values()), ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(self.history_path)

    def update(self, job_id: str, persist: bool = False, **changes):
        """Publish an atomic progress snapshot; persist state transitions only."""
        with self.lock:
            self.jobs[job_id].update(changes)
            if persist:
                self._save()

    def list_jobs(self, owner: str) -> list[dict]:
        """Return snapshots so HTTP serialization never races a download hook."""
        with self.lock:
            return copy.deepcopy(
                [
                    job
                    for job in reversed(list(self.jobs.values()))
                    if job["owner"] == owner
                ]
            )

    def owned_job(self, job_id: str, owner: str) -> dict:
        """Look up a job belonging to this browser."""
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["owner"] != owner:
                raise KeyError(job_id)
            return copy.deepcopy(job)

    def youtube_options(self) -> dict:
        """Only the process owner can supply cookies or an executable path."""
        options: dict[str, Any] = {
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 2,
            "fragment_retries": 2,
            "extractor_retries": 2,
            "sleep_interval_requests": 1.5,
            "noplaylist": True,
        }
        options.update(get_local_deno_yt_dlp_options())
        if self.settings.get("cookie_file"):
            options["cookiefile"] = self.settings["cookie_file"]
        ffmpeg = self.settings.get("ffmpeg", "ffmpeg")
        if ffmpeg == "ffmpeg":
            ffmpeg = get_ffmpeg_path()
        if ffmpeg:
            options["ffmpeg_location"] = str(ffmpeg)
        return options

    def preview(self, url: str, scope: str = "auto") -> dict:
        """Fetch metadata and one thumbnail without downloading or playing media."""
        source, kind, url = normalize_url(url, scope)
        if source == "youtube":
            options = {
                **self.youtube_options(),
                "extract_flat": "in_playlist",
                "skip_download": True,
                "playlistend": 1,
                "noplaylist": kind != "playlist",
            }
            with self.limiter.slot(skip_cooldown=True), YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Não foi possível carregar esse conteúdo.")
            return {
                "url": url,
                "source": source,
                "kind": kind,
                "title": info.get("title") or "YouTube",
                "author": info.get("uploader") or info.get("channel") or "YouTube",
                "thumbnail": thumbnail(info),
                "count": info.get("playlist_count") if kind == "playlist" else 1,
            }
        meta, songs = spotify_collection(url, kind)
        return {
            "url": url,
            "source": source,
            "kind": kind,
            "title": meta["name"],
            "author": meta.get("author_name")
            or (songs[0].artist if songs else "Spotify"),
            "thumbnail": meta.get("cover_url") or (songs[0].cover_url if songs else ""),
            "count": len(songs),
        }

    def submit(
        self,
        owner: str,
        url: str,
        scope: str,
        media_format: str,
        quality: str,
        title: str = "",
        cover: str = "",
    ) -> dict:
        """Queue a validated URL, deduplicating active requests for this browser."""
        source, kind, url = normalize_url(url, scope)
        if source == "spotify" and media_format == "mp4":
            raise ValueError("Para links do Spotify, escolha um formato de áudio.")
        with self.lock:
            if self.closed:
                raise ValueError("A fila está sendo encerrada.")
            for job in self.jobs.values():
                if (
                    job["owner"] == owner
                    and job["url"] == url
                    and job["format"] == media_format
                    and job["quality"] == quality
                    and job["status"] not in TERMINAL
                ):
                    return copy.deepcopy(job)
            if sum(job["status"] not in TERMINAL for job in self.jobs.values()) >= 50:
                raise ValueError("A fila está cheia. Aguarde os downloads atuais.")
            job_id = uuid.uuid4().hex
            job = {
                "id": job_id,
                "owner": owner,
                "url": url,
                "source": source,
                "kind": kind,
                "title": title or url,
                "thumbnail": cover,
                "format": media_format,
                "quality": quality,
                "status": "queued",
                "message": "Na fila",
                "progress": 0,
                "completed": 0,
                "total": 0,
                "files": [],
                "errors": [],
                "items": [],
                "options": {
                    "retries": self.retries,
                    "retry_delay": self.retry_delay,
                    "bitrate": self.settings.get("bitrate", "192k"),
                    "cookie_file": self.settings.get("cookie_file"),
                },
            }
            self.jobs[job_id] = job
            self.events[job_id] = threading.Event()
            self._save()
            self.executor.submit(self._run, job_id)
            return copy.deepcopy(job)

    def cancel(self, job_id: str, owner: str):
        """Cooperatively cancel without deleting completed files."""
        job = self.owned_job(job_id, owner)
        with self.lock:
            event = self.events.get(job_id)
            if job["status"] not in TERMINAL and event:
                event.set()
                self.update(job_id, message="Cancelando…")

    def cancel_item(self, job_id: str, index: int, owner: str):
        """Cancel one song while allowing the remainder of its playlist to run."""
        self.owned_job(job_id, owner)
        with self.lock:
            event = self.item_events.get((job_id, index))
            if event:
                event.set()

    def _check(self, job_id: str, index: int | None = None):
        item_event = (
            self.item_events.get((job_id, index)) if index is not None else None
        )
        if self.events[job_id].is_set() or (item_event and item_event.is_set()):
            raise Cancelled()

    def item_progress(self, job_id: str, index: int, **changes):
        """Publish per-song progress and derive collection progress atomically."""
        with self.lock:
            job = self.jobs[job_id]
            if index >= len(job["items"]):
                return
            item = job["items"][index]
            item.update(changes)
            job["progress"] = round(
                sum(
                    100 if row["status"] in TERMINAL else row["progress"]
                    for row in job["items"]
                )
                / len(job["items"]),
                1,
            )
            job["completed"] = sum(row["status"] in TERMINAL for row in job["items"])
            job["message"] = f"{job['completed']}/{job['total']} itens processados"

    def _reserve_worker(self) -> int | None:
        with self.lock:
            if len(self.active_workers) >= self.workers or self.closed:
                return None
            worker = next(i for i in range(1, 33) if i not in self.active_workers)
            self.active_workers.add(worker)
            return worker

    def _attempt(
        self, job_id: str, item: Any, index: int, worker: int, download: Callable
    ):
        try:
            self._check(job_id, index)
            # Serialize identical media across overlapping playlists, including retries.
            key = (
                (item.get("id") if isinstance(item, dict) else item.url)
                if item
                else f"missing-{index}"
            )
            with self.lock:
                file_lock = self.file_locks.setdefault(
                    f"{key}:{self.jobs[job_id]['format']}", threading.Lock()
                )
            self.item_progress(
                job_id,
                index,
                status="running",
                worker=worker,
                message="Aguardando arquivo / fonte",
                speed=None,
                eta=None,
            )
            while not file_lock.acquire(timeout=0.2):
                self._check(job_id, index)
            try:
                self._check(job_id, index)
                download(job_id, item, index)
            finally:
                file_lock.release()
            self.item_progress(
                job_id,
                index,
                status="completed",
                progress=100,
                message="Concluído",
                worker=None,
                speed=None,
                eta=None,
            )
        finally:
            with self.lock:
                self.active_workers.discard(worker)

    def _process_items(self, job_id: str, items: list, download: Callable):
        """Coordinate retries without occupying a worker during backoff."""
        job = self.jobs[job_id]
        ready = deque(range(len(items)))
        delayed: list[tuple[float, int]] = []
        futures: dict[Future, int] = {}
        policy = job["options"]
        try:
            while ready or delayed or futures:
                self._check(job_id)
                kept = []
                for deadline, index in delayed:
                    if self.item_events[(job_id, index)].is_set():
                        self.item_progress(
                            job_id,
                            index,
                            status="cancelled",
                            message="Cancelado",
                            worker=None,
                        )
                    else:
                        kept.append((deadline, index))
                if len(kept) != len(delayed):
                    delayed = kept
                    heapq.heapify(delayed)
                while delayed and delayed[0][0] <= time.monotonic():
                    _, index = heapq.heappop(delayed)
                    ready.append(index)
                while ready:
                    index = ready[0]
                    try:
                        self._check(job_id, index)
                    except Cancelled:
                        ready.popleft()
                        self.item_progress(
                            job_id,
                            index,
                            status="cancelled",
                            message="Cancelado",
                            worker=None,
                        )
                        continue
                    worker = self._reserve_worker()
                    if worker is None:
                        break
                    ready.popleft()
                    self.item_progress(
                        job_id, index, attempt=job["items"][index]["attempt"] + 1
                    )
                    try:
                        future = self.item_executor.submit(
                            self._attempt, job_id, items[index], index, worker, download
                        )
                    except Exception:
                        with self.lock:
                            self.active_workers.discard(worker)
                        raise
                    futures[future] = index
                if futures:
                    done, _ = wait(futures, timeout=0.1, return_when=FIRST_COMPLETED)
                else:
                    self.events[job_id].wait(0.05)
                    done = set()
                for future in done:
                    index = futures.pop(future)
                    try:
                        future.result()
                    except Cancelled:
                        self.item_progress(
                            job_id,
                            index,
                            status="cancelled",
                            message="Cancelado",
                            worker=None,
                        )
                    except Exception as error:
                        row = job["items"][index]
                        if (
                            self.events[job_id].is_set()
                            or self.item_events[(job_id, index)].is_set()
                        ):
                            self.item_progress(
                                job_id,
                                index,
                                status="cancelled",
                                message="Cancelado",
                                worker=None,
                            )
                        elif row["attempt"] <= policy["retries"]:
                            delay = min(
                                300, policy["retry_delay"] * 2 ** (row["attempt"] - 1)
                            )
                            deadline = time.monotonic() + delay
                            heapq.heappush(delayed, (deadline, index))
                            self.item_progress(
                                job_id,
                                index,
                                status="waiting",
                                message=f"Nova tentativa em {delay:g}s · {error}",
                                worker=None,
                                retry_at=deadline,
                                speed=None,
                                eta=None,
                            )
                        else:
                            with self.lock:
                                job["errors"].append(
                                    {"item": index + 1, "error": str(error)}
                                )
                            self.item_progress(
                                job_id,
                                index,
                                status="failed",
                                message=str(error),
                                worker=None,
                                speed=None,
                                eta=None,
                            )
                    with self.lock:
                        self._save()
        finally:
            # Let callbacks finish before removing cancellation events or saving history.
            if futures:
                wait(futures)
            for index, row in enumerate(job["items"]):
                if row["status"] not in TERMINAL:
                    self.item_progress(
                        job_id,
                        index,
                        status="cancelled",
                        message="Cancelado",
                        worker=None,
                    )

    def _record_file(self, job_id: str, filename: str, index: int | None = None):
        path = Path(filename).resolve()
        if (
            not path.is_relative_to(self.root)
            or not path.is_file()
            or path.stat().st_size == 0
        ):
            raise RuntimeError("Arquivo de saída ausente ou vazio.")
        with self.lock:
            files = self.jobs[job_id]["files"]
            relative = str(path.relative_to(self.root))
            if relative not in files:
                files.append(relative)
            if index is not None and index < len(self.jobs[job_id]["items"]):
                self.jobs[job_id]["items"][index]["file"] = relative

    def _run(self, job_id: str):
        job = self.jobs[job_id]
        download: Callable[[str, Any, int], Any]
        try:
            self._check(job_id)
            self.update(
                job_id, persist=True, status="running", message="Carregando conteúdo…"
            )
            if job["source"] == "youtube":
                options = {
                    **self.youtube_options(),
                    "extract_flat": "in_playlist",
                    "skip_download": True,
                    "noplaylist": job["kind"] != "playlist",
                }
                with self.limiter.slot(skip_cooldown=True), YoutubeDL(options) as ydl:
                    info = ydl.extract_info(job["url"], download=False)
                if not info:
                    raise ValueError("Conteúdo não disponível.")
                items = (
                    list(info.get("entries") or [])
                    if job["kind"] == "playlist"
                    else [info]
                )
                self.update(
                    job_id,
                    title=info.get("title") or job["title"],
                    thumbnail=thumbnail(info),
                )
                download = self._youtube_item
            else:
                meta, items = spotify_collection(job["url"], job["kind"])
                self.update(
                    job_id,
                    title=meta["name"],
                    thumbnail=meta.get("cover_url")
                    or (items[0].cover_url if items else ""),
                )
                download = self._spotify_item
            if not items:
                raise ValueError("Nenhum item disponível nesse link.")
            rows = []
            for index, item in enumerate(items):
                is_video = isinstance(item, dict)
                title = (
                    (item.get("title") if is_video else getattr(item, "name", None))
                    if item
                    else None
                )
                url = (
                    f"https://www.youtube.com/watch?v={item.get('id', '')}"
                    if is_video
                    else getattr(item, "url", "")
                )
                rows.append(
                    {
                        "index": index,
                        "title": title or f"Item {index + 1}",
                        "url": url,
                        "status": "queued",
                        "progress": 0,
                        "message": "Na fila",
                        "attempt": 0,
                        "worker": None,
                        "speed": None,
                        "eta": None,
                        "file": "",
                        "retry_at": None,
                    }
                )
                self.item_events[(job_id, index)] = threading.Event()
            self.update(job_id, persist=True, total=len(items), items=rows)
            self._process_items(job_id, items, download)
            errors = len(job["errors"])
            cancelled = any(row["status"] == "cancelled" for row in rows)
            status = (
                "partial"
                if (errors or cancelled) and job["files"]
                else "failed" if errors else "cancelled" if cancelled else "completed"
            )
            self.update(
                job_id,
                persist=True,
                status=status,
                message=f"{len(job['files'])} arquivo(s) salvo(s)"
                + (f" · {errors} falha(s)" if errors else ""),
            )
        except Cancelled:
            self.update(
                job_id,
                persist=True,
                status="cancelled",
                message="Cancelado. Arquivos concluídos foram mantidos.",
            )
        except Exception as error:
            logger.exception("Download failed: %s", job["url"])
            self.update(job_id, persist=True, status="failed", message=str(error))
        finally:
            with self.lock:
                self.events.pop(job_id, None)
                for key in [key for key in self.item_events if key[0] == job_id]:
                    self.item_events.pop(key, None)

    def _youtube_item(self, job_id: str, item: dict, index: int):
        self._check(job_id, index)
        if not item or not re.fullmatch(r"[A-Za-z0-9_-]{11}", item.get("id", "")):
            raise ValueError("Vídeo privado, removido ou indisponível.")
        job = self.jobs[job_id]
        directory = (
            self.root
            / "YouTube"
            / (
                urlsplit(job["url"]).query.split("=", 1)[-1]
                if job["kind"] == "playlist"
                else "Vídeos"
            )
        )
        if job["format"] == "mp4":
            directory /= (
                "Original" if job["quality"] == "best" else f"{job['quality']}p"
            )
        directory.mkdir(parents=True, exist_ok=True)

        def progress(data):
            self._check(job_id, index)
            size = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            fraction = min(0.95, data.get("downloaded_bytes", 0) / size) if size else 0
            previous = job["items"][index]["progress"] if job["items"] else 0
            self.item_progress(
                job_id,
                index,
                progress=max(previous, fraction * 100),
                message=(
                    "Convertendo / unindo áudio e vídeo"
                    if data.get("status") == "finished"
                    else "Baixando do YouTube"
                ),
                speed=data.get("speed"),
                eta=data.get("eta"),
            )

        def postprocess(data):
            self._check(job_id, index)
            self.item_progress(
                job_id,
                index,
                message="Processando · " + data.get("postprocessor", "FFmpeg"),
                speed=None,
                eta=None,
            )

        options = {
            **self.youtube_options(),
            "outtmpl": str(directory / "%(title).150s [%(id)s].%(ext)s"),
            "windowsfilenames": True,
            "overwrites": False,
            "continuedl": True,
            "progress_hooks": [progress],
            "postprocessor_hooks": [postprocess],
            "post_hooks": [lambda path: self._record_file(job_id, path, index)],
        }
        if job["format"] == "mp4":
            height = "" if job["quality"] == "best" else f"[height<={job['quality']}]"
            options.update(
                format=f"bv*[ext=mp4]{height}+ba[ext=m4a]/b[ext=mp4]{height}",
                merge_output_format="mp4",
            )
        else:
            options.update(
                format="bestaudio/best",
                postprocessors=[
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": job["format"],
                        "preferredquality": job["options"]["bitrate"].removesuffix("k"),
                    }
                ],
            )
        options["cookiefile"] = job["options"].get("cookie_file")
        self.item_progress(job_id, index, message="Aguardando YouTube")
        with self.limiter.slot(skip_cooldown=True), YoutubeDL(options) as ydl:
            self._check(job_id, index)
            result = ydl.extract_info(
                f"https://www.youtube.com/watch?v={item['id']}", download=True
            )
            # Existing files can bypass post_hooks on some yt-dlp versions.
            if result:
                path = Path(ydl.prepare_filename(result)).with_suffix(
                    f".{job['format']}"
                )
                if path.is_file():
                    self._record_file(job_id, str(path), index)
                else:
                    raise RuntimeError("O download não gerou o arquivo esperado.")
            else:
                raise RuntimeError("O YouTube não retornou um arquivo.")

    def _spotify_item(self, job_id: str, song: Song, index: int):
        self._check(job_id, index)
        job = self.jobs[job_id]
        # Include the collection ID so distinct playlists remain distinct.
        directory = self.root / "Spotify" / job["url"].rsplit("/", 1)[-1]
        directory.mkdir(parents=True, exist_ok=True)
        settings = {
            **self.settings,
            "format": job["format"],
            "output": str(directory / "{artists} - {title}.{output-ext}"),
            "overwrite": "skip",
            "threads": 1,
            "simple_tui": True,
            "print_errors": False,
            "save_errors": None,
            "bitrate": job["options"]["bitrate"],
            "cookie_file": job["options"].get("cookie_file"),
        }
        target = create_file_name(
            song, settings["output"], job["format"], settings.get("restrict")
        )
        existed = target.is_file() and target.stat().st_size > 0
        if target.is_file() and not existed:
            target.unlink()
        downloader = Downloader(
            settings=cast(DownloaderOptions, settings),
            request_limiter=self.limiter,
            provider_limiters=self.service_limiters,
        )

        def progress(tracker, message):
            self._check(job_id, index)
            stages = {
                "Getting lyrics": "Buscando letras",
                "Searching for song": "Buscando música",
                "Getting audio meta": "Obtendo áudio",
                "Downloading": "Baixando",
                "Converting": "Convertendo",
                "Embedding metadata": "Gravando metadados",
                "Done": "Concluído",
                "Skipped": "Já existe",
                "Error": "Falha",
            }
            self.item_progress(
                job_id,
                index,
                progress=min(99, tracker.progress),
                message=stages.get(message, message)
                + (
                    f" · {tracker.provider}"
                    if getattr(tracker, "provider", None)
                    else ""
                ),
            )

        downloader.progress_handler.update_callback = progress
        try:
            _, path = downloader.download_song(copy.copy(song))
            self._check(job_id, index)
            if path is None or downloader.errors:
                raise RuntimeError(
                    "; ".join(downloader.errors) or "Download sem arquivo de saída."
                )
            self._record_file(job_id, str(path), index)
        except Exception:
            if not existed:
                target.unlink(missing_ok=True)
            raise
        finally:
            downloader.progress_handler.close()
            downloader.loop.run_until_complete(downloader.loop.shutdown_asyncgens())
            downloader.loop.run_until_complete(
                downloader.loop.shutdown_default_executor()
            )
            downloader.loop.close()

    def close(self):
        """Cancel outstanding work before the application shuts down."""
        with self.lock:
            self.closed = True
            for event in self.events.values():
                event.set()
            for job in self.jobs.values():
                if job["status"] == "queued":
                    job.update(status="cancelled", message="Cancelado ao fechar")
            self._save()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.item_executor.shutdown(wait=False)
