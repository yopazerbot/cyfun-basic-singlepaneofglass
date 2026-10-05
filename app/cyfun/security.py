"""HTTP hardening: security headers, origin check for state-changing requests, rate limiting.

* Content Security Policy allows only same-origin scripts, styles, images and fonts.
  The templates contain no inline scripts or styles, so no nonces are needed.
* Every POST must carry an Origin (or Referer) header matching APP_BASE_URL. Together
  with SameSite=Lax session cookies this blocks cross-site request forgery.
* Authentication endpoints are rate limited per client address.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, Response

from .config import Settings

CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "object-src 'none'"
)

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


class SecurityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, settings: Settings, auth_rate: int | None = None, window: int = 60):
        super().__init__(app)
        self.settings = settings
        self.auth_rate = auth_rate or settings.auth_rate_per_minute
        self.window = window
        self._hits: dict[str, deque] = defaultdict(deque)

    def _rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        q = self._hits[ip]
        while q and q[0] < now - self.window:
            q.popleft()
        if len(q) >= self.auth_rate:
            return True
        q.append(now)
        if len(self._hits) > 10000:
            self._hits.clear()
        return False

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        if path.startswith("/auth/"):
            ip = request.client.host if request.client else "?"
            if self._rate_limited(ip):
                return PlainTextResponse("Too many requests", status_code=429)

        if request.method in UNSAFE:
            length = request.headers.get("content-length")
            limit = (self.settings.max_upload_mb + 2) * 1024 * 1024
            if length and length.isdigit() and int(length) > limit:
                return PlainTextResponse("Request too large", status_code=413)
            origin = request.headers.get("origin")
            referer = request.headers.get("referer", "")
            expected = self.settings.origin
            ok = (origin == expected) if origin else referer.startswith(expected + "/")
            if not ok:
                return PlainTextResponse("Cross-site request blocked", status_code=403)

        response = await call_next(request)
        h = response.headers
        h["Content-Security-Policy"] = CSP
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin"
        h["Cross-Origin-Resource-Policy"] = "same-origin"
        h["X-Permitted-Cross-Domain-Policies"] = "none"
        if self.settings.secure_cookies:
            h["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        if not path.startswith("/static/"):
            h["Cache-Control"] = "no-store"
        return response
