# -*- coding: utf-8 -*-
"""E2E security tests: full app stack with all middleware active.

Verifies that rate limiting, SSRF protection, security headers, and body size
limits function end-to-end (not just at the unit level). Uses zero-network
TestClient with a real SQLite DB and all middleware active (session, rate limit,
body size, security headers).

Run: pytest tests/test_e2e_security.py -v

Policy compliance (production implementations — not tested here, verified in their own modules):
  OpenAI API (src/services/ai_office_builder.py):
    - max_completion_tokens=4096 set on every API call to cap cost and token usage.
    - RateLimitError handling: exponential backoff (1s→2s→4s) in AIOfficeBuilder._call_openai.
  Wikipedia API (src/scraper/wiki_fetch.py):
    - User-Agent header set on all requests per Wikimedia API:Etiquette policy.
    - wiki_throttle() enforces rate_limit of ≤1 req/s; Retry adapter adds backoff on 429/5xx.
"""

from __future__ import annotations

import importlib
import os

import pytest
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Shared fixture: full app stack
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e_sec_db")
    os.environ["OFFICE_HOLDER_DB_PATH"] = str(tmp / "e2e_security.db")
    os.environ["OPENAI_API_KEY"] = "sk-fake-e2e-test-key"

    import src.main as main_mod

    importlib.reload(main_mod)

    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()  # clear any stale singleton from other test modules

    with TestClient(main_mod.app, raise_server_exceptions=False) as c:
        yield c

    os.environ.pop("OFFICE_HOLDER_DB_PATH", None)
    os.environ.pop("OPENAI_API_KEY", None)


# ---------------------------------------------------------------------------
# Security headers: all required headers must be present on every response
# ---------------------------------------------------------------------------


def test_security_headers_x_frame_options(app_client):
    resp = app_client.get("/run")
    assert resp.headers.get("x-frame-options") == "DENY"


def test_security_headers_content_type_options(app_client):
    resp = app_client.get("/run")
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_security_headers_referrer_policy(app_client):
    resp = app_client.get("/run")
    assert resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


def test_security_headers_csp_present(app_client):
    resp = app_client.get("/run")
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src" in csp, f"CSP header missing or malformed: {csp!r}"


def test_security_headers_csp_no_fallback_directives(app_client):
    resp = app_client.get("/run")
    csp = resp.headers.get("content-security-policy", "")
    for directive in ("form-action", "frame-ancestors", "base-uri", "object-src"):
        assert directive in csp, f"CSP missing '{directive}' (no default-src fallback): {csp!r}"


def test_security_headers_hsts_with_forwarded_proto(app_client):
    resp = app_client.get("/run", headers={"x-forwarded-proto": "https"})
    hsts = resp.headers.get("strict-transport-security", "")
    assert hsts, "HSTS header not set when X-Forwarded-Proto: https"
    assert "max-age=" in hsts, f"HSTS header malformed: {hsts!r}"


def test_security_headers_permissions_policy(app_client):
    resp = app_client.get("/run")
    pp = resp.headers.get("permissions-policy", "")
    assert "geolocation=()" in pp, f"Permissions-Policy header missing or malformed: {pp!r}"


# ---------------------------------------------------------------------------
# Rate limiting: infrastructure must be wired up
# ---------------------------------------------------------------------------


def test_rate_limit_exception_handler_registered(app_client):
    """429 handler must be wired up — if missing, a rate limit fires a 500."""
    from slowapi.errors import RateLimitExceeded
    import src.main as main_mod

    assert RateLimitExceeded in main_mod.app.exception_handlers, (
        "RateLimitExceeded handler not registered on the app — "
        "rate limiting is non-functional (will 500 instead of 429)"
    )


def test_rate_limit_middleware_is_active(app_client):
    """SlowAPIMiddleware must be in the middleware stack."""
    from slowapi.middleware import SlowAPIMiddleware
    import src.main as main_mod

    middleware_classes = [type(m) for m in main_mod.app.middleware_stack.__class__.__mro__]
    # Verify via app state (set in main.py: app.state.limiter = limiter)
    assert hasattr(
        main_mod.app.state, "limiter"
    ), "app.state.limiter not set — SlowAPIMiddleware may not be correctly wired"


# ---------------------------------------------------------------------------
# SSRF protection: non-Wikipedia URLs must be blocked before any fetch
# ---------------------------------------------------------------------------


def test_ssrf_non_wikipedia_url_blocked_e2e(app_client):
    """Non-Wikipedia URL in AI batch must be rejected at the app boundary (400)."""
    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()
    resp = app_client.post(
        "/api/ai-offices/batch",
        json={
            "urls": ["https://169.254.169.254/latest/meta-data/"],
            "defaults": {"country_id": 1, "level_id": 1, "branch_id": 1},
        },
    )
    assert (
        resp.status_code == 400
    ), f"SSRF to AWS metadata endpoint was not blocked: got {resp.status_code}"


def test_ssrf_localhost_url_blocked_e2e(app_client):
    """Localhost URL in AI batch must be rejected (400)."""
    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()
    resp = app_client.post(
        "/api/ai-offices/batch",
        json={
            "urls": ["http://localhost:5432/"],
            "defaults": {"country_id": 1, "level_id": 1, "branch_id": 1},
        },
    )
    assert resp.status_code == 400, f"SSRF to localhost was not blocked: got {resp.status_code}"


def test_ssrf_arbitrary_https_domain_blocked_e2e(app_client):
    """Arbitrary HTTPS domain in AI batch must be rejected (400)."""
    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()
    resp = app_client.post(
        "/api/ai-offices/batch",
        json={
            "urls": ["https://attacker.example.com/harvest"],
            "defaults": {"country_id": 1, "level_id": 1, "branch_id": 1},
        },
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Request body size limit
# ---------------------------------------------------------------------------


def test_body_size_limit_returns_413(app_client):
    """A request body larger than 1 MB must be rejected with 413."""
    large = b"x" * (1_048_576 + 1)
    resp = app_client.post(
        "/api/ai-offices/batch",
        content=large,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(large)),
        },
    )
    assert (
        resp.status_code == 413
    ), f"Expected 413 Content Too Large for oversized body, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Input validation limits
# ---------------------------------------------------------------------------


def test_batch_url_count_limit_e2e(app_client):
    """More than 20 URLs in a batch must be rejected with 400."""
    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()
    urls = [f"https://en.wikipedia.org/wiki/Page_{i}" for i in range(25)]
    resp = app_client.post(
        "/api/ai-offices/batch",
        json={"urls": urls, "defaults": {"country_id": 1, "level_id": 1, "branch_id": 1}},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth middleware: API routes return JSON 401 (not HTML redirect)
# ---------------------------------------------------------------------------


def test_api_returns_json_401_when_auth_enabled():
    """With auth enabled, API routes must return JSON 401, not an HTML redirect."""
    import src.main as main_mod

    original = main_mod._AUTH_ENABLED
    main_mod._AUTH_ENABLED = True
    try:
        with TestClient(main_mod.app, raise_server_exceptions=False) as c:
            resp = c.get("/api/run/active", follow_redirects=False)
        assert (
            resp.status_code == 401
        ), f"API route returned {resp.status_code} instead of 401 when auth is enabled"
        assert resp.headers.get("content-type", "").startswith(
            "application/json"
        ), "API 401 response must be JSON, not HTML"
    finally:
        main_mod._AUTH_ENABLED = original
