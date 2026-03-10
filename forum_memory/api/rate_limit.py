"""Rate limiting setup using slowapi.

Limits apply per employee (X-Employee-Id header), falling back to client IP.
"""
from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _rate_limit_key(request: Request) -> str:
    """Use X-Employee-Id as the rate limit key; fall back to client IP."""
    employee_id = request.headers.get("X-Employee-Id", "").strip()
    return employee_id if employee_id else get_remote_address(request)


# Single shared limiter instance (in-memory, single-process).
limiter = Limiter(key_func=_rate_limit_key)
