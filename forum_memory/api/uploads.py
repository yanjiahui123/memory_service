"""File upload API routes.

Supports two storage backends:
  - OBS (obs_enabled=True): images stored on Huawei OBS, served via backend proxy
  - Local disk (default): images saved to ``upload_dir``, served via StaticFiles

Both backends return ``/api/uploads/{filename}`` as the URL so the frontend
and extraction pipeline always use the same path format.
"""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import Response

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/uploads", tags=["uploads"])

# SVG 已移除：可内嵌恶意 JS，StaticFiles 直接 serve 会导致 XSS
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

_EXT_MEDIA_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


# ------------------------------------------------------------------
# GET  /uploads/{filename}  — image proxy (OBS) or local read
# ------------------------------------------------------------------

@router.get("/{filename}")
def serve_image(filename: str):
    """Serve uploaded image — from OBS when enabled, local disk otherwise."""
    settings = get_settings()
    media_type = _guess_media_type(filename)

    if settings.obs_enabled:
        return _serve_from_obs(filename, media_type)
    return _serve_from_local(filename, media_type, settings)


def _serve_from_obs(filename: str, media_type: str) -> Response:
    from forum_memory.services.obs_service import get_image_bytes
    try:
        data = get_image_bytes(filename)
    except Exception as e:
        logger.warning("OBS image not found: %s — %s", filename, e)
        raise HTTPException(404, "Image not found") from e
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _serve_from_local(filename: str, media_type: str, settings) -> Response:
    filepath = Path(settings.upload_dir) / filename
    if not filepath.is_file():
        raise HTTPException(404, "Image not found")
    return Response(
        content=filepath.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _guess_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return _EXT_MEDIA_MAP.get(ext, "application/octet-stream")


# ------------------------------------------------------------------
# POST /uploads  — upload new image
# ------------------------------------------------------------------

@router.post("")
def upload_file(file: UploadFile):
    settings = get_settings()
    max_bytes = settings.upload_max_size_mb * 1024 * 1024

    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(400, f"Unsupported file type: {file.content_type}")

    content = file.file.read()
    if len(content) > max_bytes:
        raise HTTPException(400, f"File too large (max {settings.upload_max_size_mb}MB)")

    ext = Path(file.filename or "image.png").suffix or ".png"

    if settings.obs_enabled:
        return _upload_to_obs(content, ext)
    return _upload_to_local(content, ext, settings)


def _upload_to_obs(content: bytes, ext: str) -> dict:
    """Upload image to OBS, return relative ``/api/uploads/`` URL."""
    from forum_memory.services.obs_service import upload_image
    try:
        filename = upload_image(content, ext)
    except Exception as e:
        logger.error("OBS upload failed: %s", e)
        raise HTTPException(500, "Image upload to OBS failed") from e
    return {"url": f"/api/uploads/{filename}", "filename": filename}


def _upload_to_local(content: bytes, ext: str, settings) -> dict:
    """Fallback: save image to local uploads directory."""
    filename = f"{uuid.uuid4().hex}{ext}"
    upload_dir = Path(settings.upload_dir)
    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / filename).write_bytes(content)
    except OSError as e:
        logger.error("Failed to save uploaded file: %s", e)
        raise HTTPException(500, "File save failed") from e
    return {"url": f"/api/uploads/{filename}", "filename": filename}
