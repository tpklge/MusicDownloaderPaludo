"""JSON endpoints used by the local graphical interface."""

import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from spotdl.utils.web import app_state
from spotdl.web.media import DownloadManager

router = APIRouter(prefix="/api/gui", tags=["Graphical interface"])
OWNER_PATTERN = r"^[A-Za-z0-9_-]{16,64}$"
MANAGER_LOCK = threading.Lock()


class DownloadRequest(BaseModel):
    """Only media choices are editable from the browser."""

    owner: str = Field(pattern=OWNER_PATTERN)
    url: str = Field(min_length=1, max_length=2048)
    scope: Literal["auto", "video", "playlist"] = "auto"
    format: Literal["mp3", "m4a", "flac", "mp4"] = "mp3"
    quality: Literal["720", "1080", "best"] = "1080"
    title: str = Field(default="", max_length=500)
    thumbnail: str = Field(default="", max_length=2048)


def manager(request: Request) -> DownloadManager:
    """Create the queue lazily so unrelated API routes have no side effects."""
    with MANAGER_LOCK:
        if not hasattr(request.app.state, "media_manager"):
            root = getattr(request.app.state, "media_output", Path.cwd() / "downloads")
            request.app.state.media_manager = DownloadManager(
                root, app_state.downloader_settings
            )
    return request.app.state.media_manager


@router.get("/config")
def config(request: Request):
    """Show the actual permanent destination selected by the process owner."""
    return {"output": str(manager(request).root), "parallel": 2}


@router.get("/preview")
def preview(
    request: Request,
    url: str = Query(max_length=2048),
    scope: Literal["auto", "video", "playlist"] = "auto",
):
    """Read metadata in FastAPI's thread pool, keeping the server responsive."""
    try:
        return manager(request).preview(url, scope)
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(
            502, detail=f"Não foi possível carregar a prévia: {error}"
        ) from error


@router.get("/jobs")
def jobs(request: Request, owner: str = Query(pattern=OWNER_PATTERN)):
    """List this browser's queue and persisted history."""
    return manager(request).list_jobs(owner)


@router.post("/jobs", status_code=202)
def submit(request: Request, body: DownloadRequest):
    """Start a background job without holding the HTTP request open."""
    try:
        return manager(request).submit(
            body.owner,
            body.url,
            body.scope,
            body.format,
            body.quality,
            body.title,
            body.thumbnail,
        )
    except ValueError as error:
        raise HTTPException(400, detail=str(error)) from error


@router.post("/jobs/{job_id}/cancel")
def cancel(request: Request, job_id: str, owner: str = Query(pattern=OWNER_PATTERN)):
    """Request cooperative cancellation."""
    try:
        manager(request).cancel(job_id, owner)
    except KeyError as error:
        raise HTTPException(404, detail="Download não encontrado.") from error
    return {"message": "Cancelamento solicitado."}


@router.get("/jobs/{job_id}/files/{index}")
def download_file(
    request: Request, job_id: str, index: int, owner: str = Query(pattern=OWNER_PATTERN)
):
    """Serve only files recorded for this job, confined to the download directory."""
    queue = manager(request)
    try:
        job = queue.owned_job(job_id, owner)
        if index < 0:
            raise IndexError(index)
        path = (queue.root / job["files"][index]).resolve()
        if not path.is_relative_to(queue.root) or not path.is_file():
            raise IndexError(index)
    except (KeyError, IndexError) as error:
        raise HTTPException(404, detail="Arquivo não encontrado.") from error
    return FileResponse(path, filename=path.name)
