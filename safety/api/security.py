"""Security headers on every response (F12).

Set in the app rather than in nginx so they ship through the normal reviewed,
CI-tested, auto-deployed path, reach dev and prod identically, and can be
asserted in tests. Responses nginx generates itself (its own 429/502) carry no
HTML, so leaving those without headers is acceptable.

Written against the raw ASGI interface, in the style of ratelimit.py: it only
adds headers to `http.response.start`, and never touches a body. setdefault()
throughout, so a route that sets its own value wins.
"""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from safety.config import settings

# What the map needs, and nothing else:
#   script/style from self and unpkg (pinned with SRI in index.html);
#   'unsafe-inline' styles only, because app.js builds style="width:...%" bars
#   and the legend gradient through innerHTML -- scripts stay strictly external;
#   Carto basemap (verified 2026-10-07): style on basemaps.cartocdn.com; sprite,
#   glyphs and tiles.json on tiles.basemaps.cartocdn.com; vector tiles on
#   tiles-{a..d}.basemaps.cartocdn.com; raster fallback on {a,b,c}.basemaps.cartocdn.com
#   -- all fetched (connect-src), images also allowed (img-src);
#   MapLibre's blob: workers; geolocation for "Locate me" (Permissions-Policy).
APP_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' https://unpkg.com",
        "style-src 'self' 'unsafe-inline' https://unpkg.com",
        "img-src 'self' data: blob: https://*.basemaps.cartocdn.com",
        "connect-src 'self' https://basemaps.cartocdn.com https://*.basemaps.cartocdn.com",
        "worker-src 'self' blob:",
        "child-src blob:",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)

# FastAPI's /docs and /redoc pull swagger-ui/redoc from jsdelivr with an inline
# bootstrap script; they get their own, looser policy rather than loosening the map's.
# Hosts from fastapi/openapi/docs.py (0.141.1): cdn.jsdelivr.net (swagger-ui, redoc),
# fastapi.tiangolo.com (favicon), fonts.googleapis.com (redoc's Montserrat/Roboto CSS,
# whose files come from fonts.gstatic.com). validator.swagger.io is Swagger UI's
# default validator badge image. cdn.redoc.ly is not referenced and is omitted.
DOCS_CSP = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
        "style-src-elem 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: https://fastapi.tiangolo.com https://validator.swagger.io",
        "worker-src 'self' blob:",
        "object-src 'none'",
        "base-uri 'self'",
        "frame-ancestors 'none'",
    ]
)


def _is_docs(path: str) -> bool:
    # /docs/oauth2-redirect included; /openapi.json gets APP_CSP, harmless for JSON.
    return path == "/docs" or path.startswith("/docs/") or path == "/redoc"


class SecurityHeadersMiddleware:
    """Adds CSP, nosniff, frame, referrer and permissions headers; HSTS on https."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        csp = DOCS_CSP if _is_docs(scope.get("path", "")) else APP_CSP
        # uvicorn sets the scheme from nginx's X-Forwarded-Proto (trusted from
        # 127.0.0.1 only), so this is https exactly for the public sites --
        # never on plain-http CI, local or candidate-port checks, where an HSTS
        # header would be meaningless at best.
        https = scope.get("scheme") == "https"

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("Content-Security-Policy", csp)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                # The legacy twin of frame-ancestors, for browsers without CSP 2.
                headers.setdefault("X-Frame-Options", "DENY")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault(
                    "Permissions-Policy", "camera=(), microphone=(), geolocation=(self)"
                )
                if https:
                    # No includeSubDomains, no preload: the hosts are subdomains
                    # of ddns.net, which this project does not own.
                    headers.setdefault(
                        "Strict-Transport-Security",
                        f"max-age={settings.hsts_max_age_seconds}",
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)
