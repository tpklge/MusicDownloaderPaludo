#!/usr/bin/env python3

import argparse
import concurrent.futures
import copy
import heapq
import json
import logging
import math
import random
import re
import sys
import threading
import time
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rich import get_console
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Column

from spotdl.utils.local_library import LocalLibrary, song_keys
from spotdl.utils.progress import BatchedProgress, StaticBarColumn, draw_progress
from spotdl.utils.request_limiter import RequestLimiter

DEFAULT_WORKERS = 12
MAX_WORKERS = 16


def slug(value: str) -> str:
    value = re.sub(r"[^\w\- ]", "", value, flags=re.UNICODE)
    value = re.sub(r"\s+", "-", value.strip())
    return value or "playlist"


def truncate(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def normalize_spotify_url(url: str) -> tuple[str, str]:
    url = url.strip().replace(r"\:", ":")
    parsed = urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if parts[0].startswith("intl-"):
        parts = parts[1:]
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc != "open.spotify.com"
        or len(parts) != 2
        or parts[0] not in {"playlist", "album"}
        or re.fullmatch(r"[A-Za-z0-9]{22}", parts[1]) is None
    ):
        raise ValueError(
            f"URL inválida: {url}. Informe uma playlist ou álbum do Spotify."
        )
    kind, spotify_id = parts
    return kind, f"https://open.spotify.com/{kind}/{spotify_id}"


def load_one_playlist(url: str) -> dict:
    from spotdl.types.album import Album
    from spotdl.types.playlist import Playlist

    kind, url = normalize_spotify_url(url)
    collection = Album if kind == "album" else Playlist
    metadata, songs = collection.get_metadata(url)
    name = str(metadata["name"])
    for song in songs:
        song.list_name = name
        song.list_url = url
    return {"name": name, "url": url, "songs": songs}


def load_playlists(urls: list[str], progress: Progress) -> list[dict]:
    from spotdl.utils.spotify import SpotifyClient

    if not urls:
        return []

    if SpotifyClient._instance is None:
        SpotifyClient.init("", "")

    discovery_task = progress.add_task(
        "[yellow]Carregando playlists", total=len(urls), status="Spotify"
    )
    draw_progress(progress)
    loaded_by_index: dict[int, dict] = {}

    def load(index_url: tuple[int, str]) -> tuple[int, dict]:
        index, url = index_url
        try:
            return index, load_one_playlist(url)
        except Exception as exception:
            raise RuntimeError(f"Falha ao carregar {url}: {exception}") from exception

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(4, len(urls))
        ) as executor:
            pending = {executor.submit(load, item) for item in enumerate(urls)}
            while pending:
                done, pending = concurrent.futures.wait(
                    pending, timeout=0.2, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    index, playlist = future.result()
                    loaded_by_index[index] = playlist
                    progress.advance(discovery_task)
                draw_progress(progress)
    finally:
        progress.remove_task(discovery_task)

    return [loaded_by_index[index] for index in range(len(urls))]


@dataclass(frozen=True)
class RetryPolicy:
    retries: int = 4
    delay: float = 15
    max_delay: float = 300

    def wait(self, attempt: int) -> float:
        base = min(self.max_delay, self.delay * 2 ** min(attempt - 1, 16))
        return min(self.max_delay, base * random.uniform(1, 1.2))


@dataclass
class DownloadResult:
    path: Path | None = None
    skipped: bool = False
    error: str = ""


@dataclass
class DownloadRuntime:
    limiter: RequestLimiter = field(default_factory=RequestLimiter)
    cookie_file: str | None = None
    library: LocalLibrary = field(default_factory=LocalLibrary)
    service_limiters: dict[str, RequestLimiter] = field(
        default_factory=lambda: {
            "internet-archive": RequestLimiter(
                concurrency=2, min_interval=1, max_interval=2
            ),
            "soundcloud": RequestLimiter(concurrency=2, min_interval=3, max_interval=5),
        }
    )
    _locks: dict[str, threading.Lock] = field(default_factory=dict)
    _guard: threading.Lock = field(default_factory=threading.Lock)

    @contextmanager
    def song_lock(self, song, path: Path):
        # Share locks across playlists and recording aliases, as well as paths.
        keys = sorted(
            {
                *song_keys(song),
                ("filename", str(path.stem).casefold()),
                ("path", str(path.resolve())),
            }
        )
        with self._guard:
            locks = [
                self._locks.setdefault(repr(key), threading.Lock()) for key in keys
            ]
        with ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            yield


def song_output(song, output_root: Path) -> tuple[Path, str]:
    from spotdl.utils.formatter import create_file_name

    playlist_name = getattr(song, "list_name", None) or "Outras"
    template = str(
        output_root / slug(playlist_name) / "{artists} - {title}.{output-ext}"
    )
    return create_file_name(song, template, "mp3"), template


@dataclass
class Job:
    song: object
    playlist: int
    attempt: int = 0


@dataclass
class RunSummary:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[dict] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return self.downloaded + self.skipped + self.failed

    def message(self) -> str:
        return (
            f"{self.downloaded} baixadas · {self.skipped} existentes · "
            f"{self.failed} falhas"
        )


def download_one(
    song,
    output_root: Path,
    runtime: DownloadRuntime,
    on_progress: Callable[[float, str], None],
    attempt: int = 1,
) -> DownloadResult:
    from spotdl.download.downloader import Downloader

    # Downloader may fill metadata and download_url; retries start from the
    # original song so a bad cached source is searched again.
    song = copy.copy(song)
    target, template = song_output(song, output_root)

    with runtime.song_lock(song, target):
        existing = runtime.library.find(song, target)
        if existing is not None:
            runtime.library.register(existing, song_keys(song))
            return DownloadResult(path=existing, skipped=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            target.unlink()  # A zero-byte file is not a successful download.

        settings = {
            "format": "mp3",
            "output": template,
            "audio_providers": ["youtube", "internet-archive", "soundcloud"],
            "overwrite": "skip",
            "threads": 1,
            "simple_tui": True,
            "log_level": "CRITICAL",
            "print_errors": False,
            "save_errors": None,
            "cookie_file": runtime.cookie_file,
            "yt_dlp_args": (
                "--sleep-requests 1.5 --sleep-interval 5 --max-sleep-interval 10 "
                "--concurrent-fragments 1 --retries 0 --fragment-retries 0 "
                "--extractor-retries 0 --socket-timeout 30"
            ),
        }
        downloader = Downloader(
            settings=settings,
            request_limiter=runtime.limiter,
            provider_limiters=runtime.service_limiters,
        )
        downloader.progress_handler.update_callback = (
            lambda tracker, message: on_progress(
                tracker.progress,
                (
                    f"{message}: {tracker.provider}"
                    if tracker.provider and ": " not in message
                    else message
                ),
            )
        )
        try:
            _, path = downloader.download_song(song)
            if path is None or downloader.errors:
                raise RuntimeError(
                    "; ".join(downloader.errors) or "Download sem arquivo de saída"
                )
            path = Path(path)
            if not path.is_file() or path.stat().st_size == 0:
                raise RuntimeError("Arquivo de saída ausente ou vazio")
            runtime.library.register(path, song_keys(song))
            return DownloadResult(path=path)
        except Exception:
            # Conversion/metadata failures can leave an incomplete MP3 which
            # overwrite=skip would otherwise accept on the next attempt.
            target.unlink(missing_ok=True)
            raise
        finally:
            downloader.loop.run_until_complete(downloader.loop.shutdown_asyncgens())
            downloader.loop.run_until_complete(
                downloader.loop.shutdown_default_executor()
            )
            downloader.loop.close()


STAGES = {
    "Getting lyrics": "Buscando letras",
    "Searching for song": "Buscando música",
    "Getting audio meta": "Obtendo áudio",
    "Downloading": "Baixando",
    "Converting": "Convertendo",
    "Embedding metadata": "Gravando metadados",
    "Done": "Concluída",
    "Skipped": "Já existe",
    "Error": "Falha na tentativa",
}


def stage_message(message: str) -> str:
    """Keep the active source visible through search, download and conversion."""
    stage, separator, source = message.partition(": ")
    label = "Internet Archive" if source == "InternetArchive" else source
    return STAGES.get(stage, stage) + (f": {label}" if separator else "")


def run_downloads(
    playlists: list[dict],
    output_root: Path,
    workers: int,
    progress: Progress,
    runtime: DownloadRuntime,
    policy: RetryPolicy,
) -> RunSummary:
    queues = [
        deque(Job(song, index) for song in playlist["songs"])
        for index, playlist in enumerate(playlists)
    ]
    ready: deque[Job] = deque()
    while any(queues):
        for queue in queues:
            if queue:
                ready.append(queue.popleft())
    total = len(ready)
    summary = RunSummary()
    if not total:
        return summary

    scan_task = progress.add_task(
        "[yellow]Verificando arquivos locais", total=None, status="Pastas e músicas"
    )
    draw_progress(progress)

    def show_scanned(count):
        progress.update(
            scan_task, completed=count, status=f"{count} arquivos encontrados"
        )
        draw_progress(progress)

    try:
        runtime.library.scan(output_root, show_scanned)
    finally:
        progress.remove_task(scan_task)

    overall = progress.add_task("[green]Músicas processadas", total=total, status="")
    network = progress.add_task("[yellow]YouTube", total=None, status="")
    playlist_tasks = [
        progress.add_task(
            f"[blue]{escape(truncate(playlist['name'], 38))}",
            total=len(playlist["songs"]),
            status="",
        )
        for playlist in playlists
    ]
    counts = [RunSummary() for _ in playlists]
    service_tasks = {
        name: progress.add_task(
            "[yellow]"
            + ("Internet Archive" if name == "internet-archive" else "SoundCloud"),
            total=None,
            status="Aguardando",
        )
        for name in runtime.service_limiters
    }
    missing: deque[Job] = deque()
    for job in ready:
        target, _ = song_output(job.song, output_root)
        if runtime.library.find(job.song, target) is None:
            missing.append(job)
        else:
            summary.skipped += 1
            counts[job.playlist].skipped += 1
    ready = missing
    progress.update(overall, completed=summary.completed, status=summary.message())
    for index, count in enumerate(counts):
        progress.update(
            playlist_tasks[index], completed=count.completed, status=count.message()
        )
    worker_tasks = {
        worker: progress.add_task(
            f"[cyan]W{worker}: aguardando", total=100, status="Fila"
        )
        for worker in range(1, min(workers, len(ready)) + 1)
    }
    events: SimpleQueue = SimpleQueue()
    delayed: list[tuple[float, int, Job]] = []
    sequence = 0
    futures = {}
    active = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        while ready or delayed or futures:
            now = time.monotonic()
            while delayed and delayed[0][0] <= now:
                _, _, job = heapq.heappop(delayed)
                # Expired retries go behind queued songs; only the coordinator
                # waits for their deadlines, leaving download workers available.
                ready.append(job)

            for worker, task in worker_tasks.items():
                if worker in active or not ready:
                    continue
                job = ready.popleft()
                job.attempt += 1
                attempt = job.attempt
                active[worker] = (job, attempt)
                name = getattr(job.song, "display_name", None) or job.song.name
                progress.reset(
                    task,
                    total=100,
                    description=f"[cyan]W{worker}: {escape(truncate(name, 38))}",
                    status=f"T{attempt}/{policy.retries + 1} · Iniciando",
                )

                def report(value, message, worker=worker, job=job, attempt=attempt):
                    events.put((worker, job, attempt, value, message))

                future = executor.submit(
                    download_one,
                    job.song,
                    output_root,
                    runtime,
                    report,
                    attempt=attempt,
                )
                futures[future] = (worker, job)

            # Only the coordinator changes Rich tasks. Download hooks send events.
            latest = {}
            while True:
                try:
                    worker, job, attempt, value, message = events.get_nowait()
                except Empty:
                    break
                if active.get(worker) == (job, attempt):
                    latest[worker] = (attempt, value, message)
            for worker, (attempt, value, message) in latest.items():
                progress.update(
                    worker_tasks[worker],
                    completed=min(99, int(value)),
                    status=f"T{attempt}/{policy.retries + 1} · {stage_message(message)}",
                )

            if futures:
                done, _ = concurrent.futures.wait(
                    futures, timeout=0.2, return_when=concurrent.futures.FIRST_COMPLETED
                )
            else:
                time.sleep(min(0.2, max(0, delayed[0][0] - time.monotonic())))
                done = set()

            for future in done:
                worker, job = futures.pop(future)
                del active[worker]
                name = getattr(job.song, "display_name", None) or job.song.name
                try:
                    result = future.result()
                    if result.path is None:
                        raise RuntimeError(
                            result.error or "Download sem arquivo de saída"
                        )
                except Exception as error:
                    message = str(error)
                    if job.attempt <= policy.retries:
                        delay = policy.wait(job.attempt)
                        sequence += 1
                        heapq.heappush(
                            delayed, (time.monotonic() + delay, sequence, job)
                        )
                        status = f"Nova tentativa em {delay:.0f}s"
                    else:
                        summary.failed += 1
                        counts[job.playlist].failed += 1
                        summary.errors.append(
                            {
                                "playlist": playlists[job.playlist]["name"],
                                "song": name,
                                "url": getattr(job.song, "url", ""),
                                "attempts": job.attempt,
                                "error": message,
                            }
                        )
                        status = "Falha definitiva"
                else:
                    if result.skipped:
                        summary.skipped += 1
                        counts[job.playlist].skipped += 1
                    else:
                        summary.downloaded += 1
                        counts[job.playlist].downloaded += 1
                    status = "Já existe" if result.skipped else "Concluída"
                progress.update(
                    worker_tasks[worker],
                    completed=0,
                    description=f"[cyan]W{worker}: livre",
                    status=status,
                )
                progress.stop_task(worker_tasks[worker])

            for index, count in enumerate(counts):
                progress.update(
                    playlist_tasks[index],
                    completed=count.completed,
                    status=count.message(),
                )
            progress.update(
                overall,
                completed=summary.completed,
                status=f"{summary.message()} · {len(delayed)} em espera",
            )
            active_requests, cooldown = runtime.limiter.state
            network_status = (
                f"Pausa global {math.ceil(cooldown)}s"
                if cooldown
                else f"{active_requests}/{runtime.limiter.concurrency} operações"
            )
            retry = (
                f" · próxima tentativa {max(0, math.ceil(delayed[0][0] - time.monotonic()))}s"
                if delayed
                else ""
            )
            progress.update(
                network,
                status=f"{network_status}{retry}",
            )
            for name, limiter in runtime.service_limiters.items():
                active_requests, cooldown = limiter.state
                status = (
                    f"Pausa {math.ceil(cooldown)}s"
                    if cooldown
                    else f"{active_requests}/{limiter.concurrency} operações"
                )
                progress.update(service_tasks[name], status=status)
            draw_progress(progress)

    progress.update(network, total=1, completed=1, status="Encerrado")
    for task in service_tasks.values():
        progress.update(task, total=1, completed=1, status="Encerrado")
    for task in worker_tasks.values():
        progress.update(task, visible=False)
    draw_progress(progress, force=True)
    return summary


def finite_nonnegative(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError(
            "Informe um número finito maior ou igual a zero"
        )
    return number


def create_progress(console: Console | None = None) -> BatchedProgress:
    """Keep text/bar positions stable while committing only changed UI frames."""
    console = console or get_console()
    columns = [
        TextColumn(
            "{task.description}",
            table_column=Column(
                width=max(14, min(42, int(console.width * 0.28))),
                overflow="ellipsis",
                no_wrap=True,
            ),
        ),
        StaticBarColumn(bar_width=None),
        MofNCompleteColumn(),
        TextColumn(
            "{task.fields[status]}",
            table_column=Column(
                width=max(18, min(68, int(console.width * 0.36))),
                overflow="ellipsis",
                no_wrap=True,
            ),
        ),
        TimeElapsedColumn(),
    ]
    if console.width >= 110:
        columns.append(TimeRemainingColumn())
    return BatchedProgress(*columns, console=console, refresh_per_second=4, expand=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Baixa playlists e álbuns do Spotify em paralelo"
    )
    parser.add_argument("playlists", nargs="+", metavar="SPOTIFY_URL")
    parser.add_argument("--output", type=Path, default=Path("downloads/playlists"))
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        choices=range(1, MAX_WORKERS + 1),
        metavar="N",
        help="Workers (padrão: 12)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=4,
        help="Novas tentativas por música, mínimo 4 (padrão: 4)",
    )
    parser.add_argument(
        "--retry-delay",
        type=finite_nonnegative,
        default=15,
        help="Espera inicial em segundos (padrão: 15)",
    )
    parser.add_argument(
        "--retry-max-delay",
        type=finite_nonnegative,
        default=300,
        help="Limite da espera entre tentativas (padrão: 300)",
    )
    parser.add_argument(
        "--youtube-concurrency",
        type=int,
        choices=range(1, 5),
        default=2,
        help="Operações simultâneas no YouTube (padrão: 2)",
    )
    parser.add_argument(
        "--request-interval",
        type=finite_nonnegative,
        default=5,
        help="Intervalo mínimo global entre operações do YouTube (padrão: 5s)",
    )
    parser.add_argument(
        "--max-request-interval",
        type=finite_nonnegative,
        default=10,
        help="Intervalo máximo global entre operações do YouTube (padrão: 10s)",
    )
    parser.add_argument(
        "--cooldown",
        type=finite_nonnegative,
        default=120,
        help="Pausa inicial do YouTube após bloqueio (padrão: 120s)",
    )
    parser.add_argument(
        "--cookie-file",
        type=Path,
        help="Cookies Netscape para conteúdo que exige autenticação",
    )
    args = parser.parse_args()
    if args.retries < 4:
        parser.error("--retries deve ser pelo menos 4 (5 tentativas totais)")
    if not 0 < args.retry_delay <= args.retry_max_delay:
        parser.error("Use 0 < --retry-delay <= --retry-max-delay")
    if args.request_interval <= 0 or args.max_request_interval < args.request_interval:
        parser.error("Use 0 < --request-interval <= --max-request-interval")
    if not 0 < args.cooldown <= 900:
        parser.error("--cooldown deve estar entre 0 e 900 segundos")
    if args.cookie_file and not args.cookie_file.is_file():
        parser.error("Arquivo de cookies não encontrado")

    args.output.mkdir(parents=True, exist_ok=True)
    # Keep per-attempt spotDL logs from breaking the shared terminal display.
    logging.getLogger("spotdl").setLevel(logging.CRITICAL)
    runtime = DownloadRuntime(
        RequestLimiter(
            args.youtube_concurrency,
            args.request_interval,
            args.max_request_interval,
            args.cooldown,
        ),
        str(args.cookie_file.resolve()) if args.cookie_file else None,
    )
    policy = RetryPolicy(args.retries, args.retry_delay, args.retry_max_delay)
    progress = create_progress()
    with progress:
        try:
            playlists = load_playlists(args.playlists, progress)
        except Exception as exception:
            progress.console.print(
                f"[red]Erro ao carregar playlists:[/] {escape(str(exception))}"
            )
            return 1
        summary = run_downloads(
            playlists, args.output, args.workers, progress, runtime, policy
        )

    report = args.output / "download-errors.json"
    report.write_text(
        json.dumps(summary.errors, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    progress.console.print(summary.message())
    if summary.failed:
        progress.console.print(f"[red]Relatório de falhas:[/] {escape(str(report))}")
    elif not summary.completed:
        progress.console.print("Nenhuma música encontrada nas playlists informadas.")
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
