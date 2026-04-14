"""Image preprocessor — replace markdown images with vision-LLM descriptions.

Scans text for markdown image syntax ``![alt](url)``, downloads the image,
**resizes + compresses** it, converts to base64, calls the vision model to
generate a text description AND search keywords.

Two outputs per image:
  - **description**: rich text for LLM context (inserted into content)
  - **keywords**: concise terms for memory/RAG search (kept separate)

This runs BEFORE the extraction pipeline so the pipeline stays text-only.
All image access goes through the backend (OBS SDK or local disk) —
no direct URL access is required by the vision model.
"""

import base64
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from forum_memory.config import get_settings
from forum_memory.providers.base import LLMProvider

logger = logging.getLogger(__name__)

# Matches ![alt text](url)
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# Vision model doesn't need full resolution — 1280px longest side is enough
# for OCR, architecture diagrams, and error screenshots.
_MAX_VISION_SIDE = 1280
_JPEG_QUALITY = 85


@dataclass
class ImageEnrichResult:
    """Result of image enrichment for a piece of text.

    - enriched_text: original text with images replaced by ``[图片内容: ...]``
    - search_terms: concise keywords from ALL images, for memory/RAG search
    """

    enriched_text: str
    search_terms: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_with_image_descriptions(text: str, llm: LLMProvider) -> ImageEnrichResult:
    """Replace markdown images in *text* with vision-LLM descriptions.

    Returns an ``ImageEnrichResult`` with both the enriched text (for LLM
    context) and extracted search keywords (for memory recall).
    """
    settings = get_settings()
    if not settings.vision_enabled:
        stripped = _strip_images_to_placeholders(text)
        return ImageEnrichResult(enriched_text=stripped)

    all_keywords: list[str] = []

    def _replace(match: re.Match) -> str:
        alt = match.group(1)
        url = match.group(2)
        desc, keywords = _describe_one_image(llm, url, alt)
        if keywords:
            all_keywords.append(keywords)
        return f"\n[图片内容: {desc}]\n"

    enriched = _IMAGE_RE.sub(_replace, text)
    return ImageEnrichResult(
        enriched_text=enriched,
        search_terms=", ".join(all_keywords),
    )


def has_images(text: str) -> bool:
    """Check whether text contains any markdown images."""
    return bool(_IMAGE_RE.search(text))


def strip_image_markdown(text: str) -> str:
    """Remove markdown image syntax entirely, leaving only surrounding text."""
    return _IMAGE_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _describe_one_image(llm: LLMProvider, url: str, alt: str) -> tuple[str, str]:
    """Download image → base64 → call vision model → parse description + keywords.

    Returns (description, keywords). On failure, returns fallback text.
    """
    try:
        image_bytes = _download_image(url)
        data_uri = _to_data_uri(image_bytes)
        raw = llm.describe_image(data_uri)
        desc, keywords = _parse_vision_output(raw)
        logger.info("Vision described image (%s): %s", alt, desc[:80])
        return desc, keywords
    except NotImplementedError:
        return (alt or "图片（视觉模型未配置）"), ""
    except Exception as exc:
        logger.warning("Vision failed for %s: %s", url, exc)
        return (alt or "图片（描述生成失败）"), ""


def _parse_vision_output(raw: str) -> tuple[str, str]:
    """Parse VL model output into (description, keywords).

    Expected format::

        描述: <text>
        关键词: <kw1>, <kw2>, ...

    Falls back gracefully: if format is wrong, treat whole output as
    description and leave keywords empty.
    """
    desc_match = re.search(r"描述[:：]\s*(.+?)(?:\n关键词|\Z)", raw, re.DOTALL)
    kw_match = re.search(r"关键词[:：]\s*(.+)", raw)

    if desc_match and kw_match:
        return desc_match.group(1).strip(), kw_match.group(1).strip()
    # Model didn't follow format — use whole output as description
    return raw.strip(), ""


def _download_image(url: str) -> bytes:
    """Download image bytes from OBS, local disk, or HTTP URL."""
    settings = get_settings()

    # Relative path: /uploads/xxx.png
    if url.startswith("/uploads/"):
        filename = url.rsplit("/", 1)[-1]
        if settings.obs_enabled:
            from forum_memory.services.obs_service import get_image_bytes
            return get_image_bytes(filename)
        filepath = Path(settings.upload_dir) / filename
        return filepath.read_bytes()

    # Absolute HTTP URL (external images)
    if url.startswith(("http://", "https://")):
        import requests
        resp = requests.get(url, timeout=30, verify=False)
        resp.raise_for_status()
        return resp.content

    raise ValueError(f"Unsupported image URL format: {url}")


def _to_data_uri(image_bytes: bytes) -> str:
    """Resize, compress, and convert image bytes to a base64 data URI.

    Steps:
      1. Open with Pillow, resize so longest side ≤ 1280px
      2. Save as JPEG (quality 85) — typically 100-300 KB
      3. Base64 encode → ``data:image/jpeg;base64,...``
    """
    with Image.open(io.BytesIO(image_bytes)) as img:
        img = _resize_if_needed(img)
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=_JPEG_QUALITY)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    logger.debug(
        "Image compressed: %d KB → %d KB",
        len(image_bytes) // 1024, len(buf.getvalue()) // 1024,
    )
    return f"data:image/jpeg;base64,{b64}"


def _resize_if_needed(img: Image.Image) -> Image.Image:
    """Shrink image so longest side ≤ _MAX_VISION_SIDE. Never upscale."""
    w, h = img.size
    longest = max(w, h)
    if longest <= _MAX_VISION_SIDE:
        return img
    scale = _MAX_VISION_SIDE / longest
    new_size = (int(w * scale), int(h * scale))
    return img.resize(new_size, Image.LANCZOS)


def _strip_images_to_placeholders(text: str) -> str:
    """When vision is off, replace images with alt-text placeholders."""
    def _replace(match: re.Match) -> str:
        alt = match.group(1)
        if alt:
            return f"[图片: {alt}]"
        return "[图片]"
    return _IMAGE_RE.sub(_replace, text)
