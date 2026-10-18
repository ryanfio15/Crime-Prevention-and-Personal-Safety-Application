"""Security headers on every response (F12). No database needed: / is static,
/docs is generated, and the 429 comes from the limiter before any route runs."""

from __future__ import annotations

from fastapi.testclient import TestClient

from safety.api import main, ratelimit
from safety.api.security import APP_CSP, DOCS_CSP

ALWAYS = (
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
    "referrer-policy",
    "permissions-policy",
)


def _script_src(csp: str) -> str:
    return next(d for d in csp.split("; ") if d.startswith("script-src"))


def test_static_page_gets_the_app_policy():
    r = TestClient(main.app).get("/")
    assert r.status_code == 200
    for name in ALWAYS:
        assert name in r.headers, name
    csp = r.headers["content-security-policy"]
    assert csp == APP_CSP
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self' https://unpkg.com" in csp
    assert "'unsafe-eval'" not in csp
    assert "'unsafe-inline'" not in _script_src(csp)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_docs_get_their_own_policy():
    r = TestClient(main.app).get("/docs")
    assert r.status_code == 200
    assert r.headers["content-security-policy"] == DOCS_CSP
    assert "cdn.jsdelivr.net" in r.headers["content-security-policy"]


def test_no_hsts_over_plain_http():
    r = TestClient(main.app).get("/")
    assert "strict-transport-security" not in r.headers


def test_hsts_over_https():
    r = TestClient(main.app, base_url="https://testserver").get("/")
    assert r.headers["strict-transport-security"] == "max-age=86400"


def test_rate_limited_responses_carry_the_headers(monkeypatch):
    # Exhaust the API zone for the test client's address, then check the 429.
    monkeypatch.setattr(ratelimit, "_API_BURST", 0)
    r = TestClient(main.app).get("/api/v1/cities")
    assert r.status_code == 429
    assert r.headers["content-security-policy"] == APP_CSP
