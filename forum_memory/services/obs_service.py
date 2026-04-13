"""OBS (Object Storage Service) — image upload and proxy download.

Images are stored on OBS but served through the backend (OBS URL is not
publicly accessible).  The backend acts as a proxy: upload via SDK,
download via SDK, return bytes to the caller.
"""

import logging
import uuid

from obs import ObsClient

from forum_memory.config import get_settings

logger = logging.getLogger(__name__)

_client: ObsClient | None = None


def _get_client() -> ObsClient:
    global _client
    if _client is not None:
        return _client
    settings = get_settings()
    _client = ObsClient(
        access_key_id=settings.obs_ak,
        secret_access_key=settings.obs_sk,
        server=settings.obs_endpoint,
        path_style=True,
        signature="v2",
        is_signature_negotiation=True,
    )
    return _client


def _object_key(filename: str) -> str:
    """Build OBS object key from filename."""
    settings = get_settings()
    return f"{settings.obs_upload_prefix}/{filename}"


def upload_image(content: bytes, ext: str) -> str:
    """Upload image bytes to OBS.

    Returns:
        The generated filename (e.g. ``"a1b2c3.png"``), NOT a full URL.
        Callers should build ``/uploads/{filename}`` for the frontend.
    """
    settings = get_settings()
    filename = f"{uuid.uuid4().hex}{ext}"
    key = _object_key(filename)

    resp = _get_client().putObject(settings.obs_bucket, key, content)
    if resp.status >= 300:
        logger.error("OBS upload failed: status=%d, key=%s", resp.status, key)
        raise RuntimeError(f"OBS upload failed (status {resp.status})")

    logger.info("Uploaded image to OBS: key=%s", key)
    return filename


def get_image_bytes(filename: str) -> bytes:
    """Download image bytes from OBS by filename.

    This is used by the image proxy endpoint and the vision preprocessor.
    """
    settings = get_settings()
    key = _object_key(filename)
    resp = _get_client().getObject(
        settings.obs_bucket, key, loadStreamInMemory=True,
    )
    if resp.status < 300:
        return resp.body.buffer
    logger.error("OBS download failed: status=%d, key=%s", resp.status, key)
    raise RuntimeError(f"OBS download failed (status {resp.status})")
