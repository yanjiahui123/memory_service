"""Access-log middleware — logs every HTTP request to the access logger.

Output format (one line per request):
    <client_ip> "<METHOD> <path>" <status> <duration_ms>ms

On unhandled exceptions the error is captured in both access.log (as 500)
and error.log (with full traceback), then re-raised so Starlette can return
the 500 response to the client.

Skips health-check endpoints to reduce noise.
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from forum_memory.logging_config import ACCESS_LOGGER

access_logger = logging.getLogger(ACCESS_LOGGER)
error_logger = logging.getLogger(__name__)

# Paths that are polled constantly — skip to avoid log noise
_SKIP_PREFIXES = ("/health",)


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log method, path, status, and duration for every request."""

    async def dispatch(self, request: Request, call_next) -> Response:
        if _should_skip(request.url.path):
            return await call_next(request)

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            duration_ms = (time.perf_counter() - start) * 1000
            client_ip = _client_ip(request)
            # Write to error.log with full traceback
            error_logger.error(
                '%s "%s %s" 500 %.0fms — unhandled exception',
                client_ip,
                request.method,
                request.url.path,
                duration_ms,
                exc_info=True,
            )
            # Also record in access.log so the request is never silently missing
            access_logger.info(
                '%s "%s %s" 500 %.0fms',
                client_ip,
                request.method,
                request.url.path,
                duration_ms,
            )
            raise

        duration_ms = (time.perf_counter() - start) * 1000
        client_ip = _client_ip(request)
        access_logger.info(
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
