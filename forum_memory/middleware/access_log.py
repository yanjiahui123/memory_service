"""Access-log middleware — logs every HTTP request to the access logger.

Output format (one line per request):
    <client_ip> "<METHOD> <path>" <status> <duration_ms>ms

Skips health-check endpoints to reduce noise.
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from forum_memory.logging_config import ACCESS_LOGGER

logger = logging.getLogger(ACCESS_LOGGER)

# Paths that are polled constantly — skip to avoid log noise
_SKIP_PREFIXES = ("/health",)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log method, path, status, and duration for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if _should_skip(request.url.path):
            return await call_next(request)

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        client_ip = _client_ip(request)
        logger.info(
            '%s "%s %s" %s %.0fms',
            client_ip,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response


def _should_skip(path: str) -> bool:
    return any(path.startswith(p) for p in _SKIP_PREFIXES)


def _client_ip(request: Request) -> str:
    """Extract client IP, respecting X-Forwarded-For behind a reverse proxy."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "-"
