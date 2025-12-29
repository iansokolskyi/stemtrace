"""Static file serving for bundled React UI assets."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"


def get_static_router() -> APIRouter | None:
    """Create router for UI static files. Returns None if dist/ missing."""
    if not _FRONTEND_DIR.exists():
        logger.warning("Frontend dist not found at %s", _FRONTEND_DIR)
        return None

    router = APIRouter(tags=["stemtrace-ui"])
    router.mount(
        "/assets",
        StaticFiles(directory=_FRONTEND_DIR / "assets"),
        name="stemtrace-assets",
    )

    @router.get("/", response_class=HTMLResponse)
    async def serve_index() -> HTMLResponse:
        """Serve the main index.html page."""
        index_path = _FRONTEND_DIR / "index.html"
        if not index_path.exists():
            return HTMLResponse("<h1>UI not built</h1>", status_code=503)
        return HTMLResponse(index_path.read_text())

    @router.get("/{path:path}", response_model=None)
    async def serve_spa(path: str) -> FileResponse | HTMLResponse:
        """Serve static files or fall back to index.html for SPA routing."""
        file_path = _FRONTEND_DIR / path
        if file_path.exists() and file_path.is_file():
            return FileResponse(file_path)

        index_path = _FRONTEND_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text())

        return HTMLResponse("<h1>Not found</h1>", status_code=404)

    return router


def is_ui_available() -> bool:
    """Check if built UI assets exist."""
    return (_FRONTEND_DIR / "index.html").exists()
