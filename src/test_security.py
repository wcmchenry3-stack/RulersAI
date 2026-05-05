"""Security regression tests — runs as part of the normal pytest suite.

Uses FastAPI TestClient (no live server). The DB is initialised into a
temp directory; Datasette startup is suppressed so no subprocess is spawned.

Run in isolation:  pytest src/test_security.py -v
Run with marker:   pytest -m security -v

Policy compliance (production implementations — not tested here, verified in their own modules):
  OpenAI API (src/services/ai_office_builder.py):
    - max_completion_tokens=4096 set on every API call to cap cost and token usage.
    - RateLimitError handling: exponential backoff (1s→2s→4s) in AIOfficeBuilder._call_openai.
  Wikipedia API (src/scraper/wiki_fetch.py):
    - User-Agent header set on all requests per Wikimedia API:Etiquette policy.
    - wiki_throttle() enforces rate_limit of ≤1 req/s; Retry adapter adds backoff on 429/5xx.
"""

from __future__ import annotations

import io
import importlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client(tmp_path_factory, monkeypatch_module=None):
    """TestClient with a temp DB and Datasette suppressed."""
    # Use a module-scoped tmp dir so the DB is shared across all tests here.
    tmp = tmp_path_factory.mktemp("sec_db")
    db_path = tmp / "security_test.db"

    import os

    os.environ["OFFICE_HOLDER_DB_PATH"] = str(db_path)

    import src.main as main_mod

    with TestClient(main_mod.app, raise_server_exceptions=False) as c:
        yield c

    os.environ.pop("OFFICE_HOLDER_DB_PATH", None)


# ---------------------------------------------------------------------------
# H1 — Security headers
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_security_headers_present(client):
    """Every response must carry the three baseline security headers."""
    resp = client.get("/run")
    assert resp.status_code == 200
    assert (
        resp.headers.get("x-frame-options") == "DENY"
    ), "X-Frame-Options header missing or incorrect"
    assert (
        resp.headers.get("x-content-type-options") == "nosniff"
    ), "X-Content-Type-Options header missing"
    assert (
        resp.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    ), "Referrer-Policy header missing"


# ---------------------------------------------------------------------------
# H2 — Auth enforcement
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_auth_middleware_redirects_unauthenticated_requests():
    """With auth enabled, protected routes must redirect to /login."""
    import os

    prior_db = os.environ.get("OFFICE_HOLDER_DB_PATH")
    tmp_db = Path(__file__).parent.parent / "tmp_auth_test.db"
    os.environ["OFFICE_HOLDER_DB_PATH"] = str(tmp_db)

    import src.main as main_mod

    original = main_mod._AUTH_ENABLED
    main_mod._AUTH_ENABLED = True

    try:
        # allow_redirects=False so we catch the redirect itself, not its destination
        with TestClient(main_mod.app, raise_server_exceptions=False) as c:
            resp = c.get("/run", follow_redirects=False)
            assert resp.status_code in (
                301,
                302,
                307,
                308,
            ), f"Expected redirect for unauthenticated request, got {resp.status_code}"
            assert "/login" in resp.headers.get("location", ""), "Redirect must point to /login"
    finally:
        main_mod._AUTH_ENABLED = original
        if prior_db is not None:
            os.environ["OFFICE_HOLDER_DB_PATH"] = prior_db
        else:
            os.environ.pop("OFFICE_HOLDER_DB_PATH", None)
        if tmp_db.exists():
            tmp_db.unlink()


@pytest.mark.security
def test_auth_middleware_allows_public_paths():
    """Login page must remain accessible without a session even when auth is enabled."""
    import os

    prior_db = os.environ.get("OFFICE_HOLDER_DB_PATH")
    tmp_db = Path(__file__).parent.parent / "tmp_auth_pub_test.db"
    os.environ["OFFICE_HOLDER_DB_PATH"] = str(tmp_db)

    import src.main as main_mod

    original = main_mod._AUTH_ENABLED
    main_mod._AUTH_ENABLED = True

    try:
        with TestClient(main_mod.app, raise_server_exceptions=False) as c:
            resp = c.get("/login", follow_redirects=False)
            assert (
                resp.status_code == 200
            ), f"/login must be publicly accessible, got {resp.status_code}"
            head_resp = c.head("/login", follow_redirects=False)
            assert (
                head_resp.status_code == 200
            ), f"HEAD /login must return 200, got {head_resp.status_code}"
    finally:
        main_mod._AUTH_ENABLED = original
        if prior_db is not None:
            os.environ["OFFICE_HOLDER_DB_PATH"] = prior_db
        else:
            os.environ.pop("OFFICE_HOLDER_DB_PATH", None)
        if tmp_db.exists():
            tmp_db.unlink()


# ---------------------------------------------------------------------------
# H3 — Path disclosure in /offices/import
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_offices_import_error_does_not_leak_resolved_path(client):
    """A path-traversal attempt must not disclose the server's filesystem layout."""
    resp = client.post("/offices/import", data={"csv_path": "../../etc/passwd"})
    body = resp.text
    # The resolved absolute path must not appear in the response body
    assert "/etc/passwd" not in body, "Response leaks resolved path — path disclosure vulnerability"
    assert "C:\\" not in body and "C:/" not in body, "Response leaks Windows absolute path"


@pytest.mark.security
def test_offices_import_rejects_empty_path(client):
    """Empty csv_path must return a user-friendly error, not a 500."""
    resp = client.post("/offices/import", data={"csv_path": ""})
    assert resp.status_code == 200, "Should render error page (200), not crash"
    assert (
        "required" in resp.text.lower() or "error" in resp.text.lower()
    ), "Expected an error message for empty path"


# ---------------------------------------------------------------------------
# H5 — SQL injection resistance
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_sql_injection_in_offices_query_params(client):
    """SQL injection strings in query params must not crash the app or corrupt the DB."""
    payloads = [
        "'; DROP TABLE offices; --",
        "1 UNION SELECT * FROM sqlite_master --",
        "' OR '1'='1",
    ]
    for payload in payloads:
        resp = client.get(f"/offices?search_url={payload}")
        assert resp.status_code == 200, f"SQL injection payload crashed the app: {payload!r}"

    # DB must still be intact
    resp = client.get("/offices")
    assert resp.status_code == 200, "offices route broken after injection attempt"


# ---------------------------------------------------------------------------
# H6 — File upload validation
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_csv_upload_rejects_non_csv_extension(client):
    """Uploading a non-CSV file to /refs/parties/import must be rejected gracefully."""
    fake_exe = io.BytesIO(b"MZ\x90\x00")  # PE header magic bytes
    resp = client.post(
        "/refs/parties/import",
        files={"file": ("malware.exe", fake_exe, "application/octet-stream")},
    )
    # Must not be a 500; should be 200 (error page) or 400/422
    assert resp.status_code != 500, "File upload validation raised unhandled 500"
    assert resp.status_code in (
        200,
        400,
        422,
    ), f"Unexpected status {resp.status_code} for non-CSV upload"
    if resp.status_code == 200:
        assert (
            "csv" in resp.text.lower() or "error" in resp.text.lower()
        ), "Expected a validation error message for non-CSV file"


# ---------------------------------------------------------------------------
# H4 & H7 — Input validation on /api/run
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_run_api_requires_individual_ref_for_single_bio_mode(client):
    """single_bio mode without individual_ref must return 400, not start a job."""
    resp = client.post("/api/run", data={"run_mode": "single_bio", "individual_ref": ""})
    assert (
        resp.status_code == 400
    ), f"Expected 400 for missing individual_ref, got {resp.status_code}"


@pytest.mark.security
def test_run_api_invalid_run_mode_does_not_crash(client):
    """Garbage / XSS payload in run_mode must not cause a 500."""
    resp = client.post(
        "/api/run",
        data={"run_mode": "<script>alert(1)</script>", "individual_ref": ""},
    )
    assert resp.status_code != 500, f"Invalid run_mode caused unhandled 500: {resp.text[:200]}"


# ---------------------------------------------------------------------------
# H7 — Type safety on path parameters
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_integer_path_param_type_safety(client):
    """Non-integer office ID in URL must return 422 (FastAPI validation), not 500."""
    resp = client.get("/offices/not-a-number")
    assert resp.status_code in (
        404,
        422,
    ), f"Expected 404 or 422 for non-integer office ID, got {resp.status_code}"


# ---------------------------------------------------------------------------
# H8 — XSS: stored values returned as literal text
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_xss_payload_in_country_name_is_not_executed(client):
    """An XSS payload stored via the refs form must appear escaped in the response,
    not as executable HTML."""
    payload = "<script>alert('xss')</script>"
    client.post("/refs/countries/new", data={"name": payload})
    resp = client.get("/refs/countries")
    assert resp.status_code == 200
    # The raw <script> tag must NOT appear unescaped in the page
    assert "<script>alert" not in resp.text, "XSS payload was reflected unescaped"


# ---------------------------------------------------------------------------
# H9 — Open redirect prevention
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_no_open_redirect_on_saved_param(client):
    """The ?saved=1 param on refs list pages must not redirect to an external URL."""
    resp = client.get("/refs/countries?saved=https://evil.example.com", follow_redirects=False)
    # Must render the page (200), not redirect externally
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# H10 — Sensitive data not in error responses
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_404_response_does_not_leak_stack_trace(client):
    """A 404 for an unknown route must not expose a Python stack trace."""
    resp = client.get("/this/route/does/not/exist")
    assert resp.status_code == 404
    assert "Traceback" not in resp.text
    assert 'File "' not in resp.text


@pytest.mark.security
def test_malformed_body_does_not_leak_internals(client):
    """Sending garbage bytes to a form endpoint must not expose a Python stack trace."""
    resp = client.post(
        "/refs/countries/new",
        content=b"\xff\xfe<invalid encoding>",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert resp.status_code != 500, f"Malformed body caused unhandled 500: {resp.text[:200]}"
    assert "Traceback" not in resp.text
    assert 'File "' not in resp.text


# ---------------------------------------------------------------------------
# H11 — Content-Security-Policy header
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_csp_header_present(client):
    """Every response must carry a Content-Security-Policy header."""
    resp = client.get("/run")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert csp, "Content-Security-Policy header is missing"
    assert "default-src" in csp, f"CSP must contain a default-src directive, got: {csp!r}"


# ---------------------------------------------------------------------------
# H12 — Rate limit exception handler wired up
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_rate_limit_handler_registered():
    """The 429 RateLimitExceeded handler must be registered on the app.

    If it is missing, a rate-limit event would fire a 500 instead of 429.
    """
    import src.main as main_mod
    from slowapi.errors import RateLimitExceeded

    assert (
        RateLimitExceeded in main_mod.app.exception_handlers
    ), "RateLimitExceeded handler not registered on app — rate limiting is non-functional"


# ---------------------------------------------------------------------------
# H13 — SSRF: non-Wikipedia URLs rejected in AI batch
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_ssrf_non_wikipedia_url_rejected(client):
    """POST /api/ai-offices/batch must reject non-Wikipedia URLs with 400."""
    import os
    from unittest.mock import patch
    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-ssrf-test"}):
        resp = client.post(
            "/api/ai-offices/batch",
            json={
                "urls": ["https://evil.example.com/steal"],
                "defaults": {"country_id": 1, "level_id": 1, "branch_id": 1},
            },
        )
    assert (
        resp.status_code == 400
    ), f"Non-Wikipedia URL must be rejected with 400, got {resp.status_code}"
    detail = resp.json().get("detail", "")
    assert (
        "wikipedia" in detail.lower() or "url" in detail.lower()
    ), f"Error detail should reference Wikipedia URL requirement, got: {detail!r}"
    reset_ai_builder()


# ---------------------------------------------------------------------------
# H14 — Batch URL count limit
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_ai_batch_too_many_urls_rejected(client):
    """POST /api/ai-offices/batch with more than 20 URLs must return 400."""
    import os
    from unittest.mock import patch
    from src.services.orchestrator import reset_ai_builder

    reset_ai_builder()
    urls = [f"https://en.wikipedia.org/wiki/Page_{i}" for i in range(21)]
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-fake-batch-count-test"}):
        resp = client.post(
            "/api/ai-offices/batch",
            json={
                "urls": urls,
                "defaults": {"country_id": 1, "level_id": 1, "branch_id": 1},
            },
        )
    assert (
        resp.status_code == 400
    ), f"Batch of 21 URLs must be rejected with 400, got {resp.status_code}"
    reset_ai_builder()


# ---------------------------------------------------------------------------
# H15 — Request body size limit
# ---------------------------------------------------------------------------


@pytest.mark.security
def test_request_body_size_limit(client):
    """A request body larger than 1 MB must be rejected with 413."""
    large = b"x" * (1_048_576 + 1)  # 1 MB + 1 byte
    resp = client.post(
        "/api/ai-offices/batch",
        content=large,
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(len(large)),
        },
    )
    assert (
        resp.status_code == 413
    ), f"Body > 1 MB must return 413 Content Too Large, got {resp.status_code}"
