# Security Policy — Office Holder

See [~/.claude/standards/security.md](~/.claude/standards/security.md) for the universal OWASP Top 10 framework and secret hygiene rules.

## Project-Specific Implementation

All code changes must be tested against this policy before merging. The automated suite lives in
`src/test_security.py` and runs as part of the normal pytest suite:

```bash
python -m pytest src/test_security.py -v   # security tests only
pytest -m security -v                       # same, by marker
python -m pytest                            # included in full suite
```

---

## OWASP Top 10 Coverage

### A01 — Broken Access Control
- **Policy**: Every non-public route must redirect unauthenticated users to `/login` when
  `GOOGLE_CLIENT_ID` is set. Public paths are limited to `/login`, `/auth/google`,
  `/auth/google/callback`, `/static/`, and `/security.txt`.
- **Test**: `test_auth_middleware_redirects_unauthenticated_requests`,
  `test_auth_middleware_allows_public_paths`
- **Dev note**: Auth is intentionally disabled locally when `GOOGLE_CLIENT_ID` is unset. Never
  deploy without it.

### A02 — Cryptographic Failures
- **Policy**: `SECRET_KEY` env var must be set to a strong random value in production. The default
  `"dev-only-insecure-key"` value must never be used in a deployed environment.
- **Test**: Manual (env var audit before deploy). No automated test — asserting the key is secret
  would require embedding it in the test.

### A03 — Injection
- **Policy**: All database queries must use parameterised statements (`?` placeholders). No
  `f`-string SQL, no `str.format()` in queries. Filter criteria are matched as plain substrings —
  never passed to `eval`, `exec`, or a regex engine controlled by user input.
- **Test**: `test_sql_injection_in_offices_query_params` — submits classic payloads (`'; DROP TABLE
  offices; --`, `UNION SELECT`, `OR '1'='1'`) and asserts the app returns 200 with the DB intact.

### A04 — Insecure Design
- **Policy**: All subprocess calls must use list-based arguments (no `shell=True`). Wikipedia fetch
  URLs must be validated through `normalize_wiki_url()` before any HTTP call. The Datasette
  instance is bound to `127.0.0.1` only and mounted read-only (`--immutable`).
- **Rate limiting**: All API endpoints are rate-limited via SlowAPI (`slowapi`). Limits are keyed by authenticated user email (or client IP for unauthenticated routes):
  - Global default: `200/minute`
  - `POST /auth/google`, `GET /auth/google/callback`: `10/minute`
  - `POST /api/run`: `20/minute`
  - `POST /api/ai-offices/batch`: `10/minute`
  - `POST /api/gemini-research/run`: `10/minute`
- **Middleware ordering**: `SlowAPIMiddleware` must be added to the Starlette middleware stack **before** `SessionMiddleware`. FastAPI processes middleware in LIFO order — adding SlowAPIMiddleware last means it runs first on the request path. Reversing this order breaks rate limiting silently.
- **Request body size**: `limit_request_body_size` middleware caps request bodies at **1 MB** and returns HTTP 413 for oversized requests.
- **Test**: Covered by existing integration tests; `test_run_api_invalid_run_mode_does_not_crash`
  fuzzes the `run_mode` field with an XSS payload; `test_request_body_size_limit` tests the 1 MB cap; `test_rate_limit_handler_registered` asserts the SlowAPI handler is installed.

### A05 — Security Misconfiguration
- **Policy**: Every HTTP response must carry the following headers, added by `add_security_headers` middleware in `src/main.py`:
  - `X-Frame-Options: DENY`
  - `X-Content-Type-Options: nosniff`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:`
  - `Permissions-Policy: geolocation=(), microphone=(), camera=()`
  - `Strict-Transport-Security: max-age=31536000; includeSubDomains` (HTTPS connections only)
- **Test**: `test_security_headers_present`, `test_csp_header_present`

### A06 — Vulnerable and Outdated Components
- **Policy**: Dependencies are pinned in `requirements.txt`. Run `pip list --outdated` before
  each release cycle and update dependencies with known CVEs.
- **Test**: Manual / CI dependency scanning (not yet automated).

### A07 — Identification and Authentication Failures
- **Policy**: Authentication uses Google OAuth via Authlib. Sessions are signed with
  `SessionMiddleware` using a secret set via `SECRET_KEY`. Only the single email address in
  `ALLOWED_EMAIL` may log in.
- **Test**: `test_auth_middleware_redirects_unauthenticated_requests`

### A08 — Software and Data Integrity Failures
- **Policy**: CSV imports validate file extension (`.csv` only). All uploads are written to
  `tempfile.NamedTemporaryFile` and deleted after use. No deserialisation of untrusted data
  (no `pickle`, no `yaml.load` without `Loader=SafeLoader`).
- **Test**: `test_csv_upload_rejects_non_csv_extension`

### A09 — Security Logging and Monitoring Failures
- **Policy**: Scraper runs log to timestamped files under `data/logs/`. Auth failures are logged
  by FastAPI's default error handler. No PII is written to log files.
- **Test**: Out of scope for automated tests; review log output manually.

### A10 — Server-Side Request Forgery (SSRF)
- **Policy**: All outbound HTTP calls target Wikipedia's REST API (with User-Agent header per Wikimedia
  etiquette; rate-limited via `wiki_throttle()` with retry/backoff on 429/503), OpenAI API, or
  Google Gemini API only.
  `normalize_wiki_url()` enforces `https` scheme and `wikipedia.org` in the hostname before any
  request is made. User-supplied URLs in `individual_ref` are passed through this validator.
  Gemini API calls go through `src/services/gemini_vitals_researcher.py` only; OpenAI through
  `src/services/ai_office_builder.py` only; Wikipedia submit through
  `src/services/wikipedia_submit.py` only (User-Agent set per Wikimedia etiquette, rate-limited,
  `WIKIPEDIA_BOT_USERNAME`/`WIKIPEDIA_BOT_PASSWORD` never hardcoded); Claude auto-fix through
  `src/services/claude_client.py` only (`ANTHROPIC_API_KEY` never hardcoded, `max_tokens=4096`,
  exponential backoff on 429). Auto-fix proposals are gated by 7 deterministic criteria before
  any PR is created; PRs are always opened as draft.
- **AI batch endpoint**: `POST /api/ai-offices/batch` enforces per-URL validation via `validate_and_normalize_wiki_url()` before any HTTP call: max **20 URLs** per request, max **500 characters** per URL, `wikipedia.org` domain only.
- **Test**: `test_run_api_requires_individual_ref_for_single_bio_mode` (boundary check);
  `test_ssrf_non_wikipedia_url_rejected` asserts non-Wikipedia URLs are rejected;
  `test_ai_batch_too_many_urls_rejected` asserts the 20-URL limit;
  full URL validation tested in `src/scraper/test_wiki_fetch.py`.
- **Gemini API note**: Google retains prompts and responses for 55 days for abuse monitoring.
  Do not submit sensitive or personal data via the Gemini API on the free tier.

---

## Additional Controls

### Path Traversal
- **Policy**: Error messages must never include resolved absolute filesystem paths. Relative paths
  submitted via form fields are resolved to the project root — the resolved path is not returned
  to the user.
- **Test**: `test_offices_import_error_does_not_leak_resolved_path`,
  `test_offices_import_rejects_empty_path`

### Type Safety
- **Policy**: FastAPI path parameters with integer types are validated automatically (HTTP 422 on
  type mismatch). No manual casting of path params from strings in route handlers.
- **Test**: `test_integer_path_param_type_safety`

---

## Pre-commit Checklist

Before opening a PR:

- [ ] `python -m pytest` — full suite passes
- [ ] `pytest -m security -v` — all 10 security tests pass
- [ ] No new `shell=True` subprocess calls introduced
- [ ] No raw f-string SQL queries introduced
- [ ] No new public routes added without updating `_PUBLIC_PATHS` intentionally
- [ ] `SECRET_KEY` and `GOOGLE_CLIENT_ID` confirmed set in deployment environment
- [ ] New API endpoints have rate limits applied (`@limiter.limit(...)`)
- [ ] Middleware ordering not changed (SlowAPIMiddleware before SessionMiddleware)
