"""File upload API routes."""

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["uploads"])

# SVG 已移除：可内嵌恶意 JS，StaticFiles 直接 serve 会导致 XSS
ALLOWED_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


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
    filename = f"{uuid.uuid4().hex}{ext}"
    upload_dir = Path(settings.upload_dir)

    try:
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / filename).write_bytes(content)
    except OSError as e:
        logger.error("Failed to save uploaded file: %s", e)
        raise HTTPException(500, "File save failed") from e

    return {"url": f"/uploads/{filename}", "filename": filename}
