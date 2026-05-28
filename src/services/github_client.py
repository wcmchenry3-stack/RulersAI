# -*- coding: utf-8 -*-
"""
GitHub REST API client singleton.

All GitHub API calls in this project must go through this module — no direct
httpx/requests calls to api.github.com from routers, scrapers, or DB layer.

Provides:
  get_github_client() -> GitHubClient | None
      Lazy singleton. Returns None if GITHUB_TOKEN or GITHUB_REPO is not set,
      so callers can degrade gracefully without raising.

  reset_github_client() -> None
      Resets the singleton for tests.

--- Policy compliance ---

GitHub REST API:
  - Authentication: Bearer token from GITHUB_TOKEN env var (never hardcoded).
  - Rate limiting: exponential backoff on HTTP 429 (3 retries, 1 s → 2 s → 4 s).
  - Pagination: label-filter search uses per_page=10; only first page is read
    (we only need existence, not all matches).
  - GITHUB_TOKEN is never logged or included in error messages.
  See: https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api
"""

from __future__ import annotations

import logging
import os
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_GITHUB_API_BASE = "https://api.github.com"
_DEFAULT_REPO = "wcmchenry3-stack/office_holder_cursor"

_client_lock = threading.Lock()
_client: GitHubClient | None = None


class GitHubClient:
    """Thin wrapper around the GitHub REST API.

    Uses httpx for HTTP calls. All methods implement exponential backoff on
    HTTP 429 (3 retries, 1 s → 2 s → 4 s) matching the OpenAI client pattern.

    Rate limit / retry / backoff: exponential backoff on HTTP 429.
    """

    def __init__(self, token: str, repo: str) -> None:
        self._repo = repo
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_open_issue_by_label(self, label: str) -> dict | None:
        """Return the first open issue with the given label, or None.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/issues"
        params = {"labels": label, "state": "open", "per_page": 10}
        data = self._get(url, params=params)
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
        return None

    def list_open_issues_by_label(self, label: str) -> list[dict]:
        """Return all open issues with the given label.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/issues"
        params = {"labels": label, "state": "open", "per_page": 100}
        data = self._get(url, params=params)
        if data and isinstance(data, list):
            return data
        return []

    def get_default_branch_sha(self, branch: str = "dev") -> str | None:
        """Return the HEAD SHA of the given branch, or None on failure.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/git/ref/heads/{branch}"
        data = self._get(url)
        if data and isinstance(data, dict):
            return data.get("object", {}).get("sha")
        return None

    def create_branch(self, branch_name: str, from_sha: str) -> dict | None:
        """Create a new branch pointing at from_sha.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        Returns the API response dict or None on failure.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/git/refs"
        payload = {"ref": f"refs/heads/{branch_name}", "sha": from_sha}
        try:
            return self._post(url, json=payload)
        except RuntimeError:
            logger.exception("Failed to create branch %s", branch_name)
            return None

    def get_file_content(self, path: str, ref: str = "dev") -> dict | None:
        """Get file content and SHA from the repo. Returns dict with 'content', 'sha'.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/contents/{path}"
        params = {"ref": ref}
        data = self._get(url, params=params)
        if data and isinstance(data, dict) and "content" in data:
            import base64

            content = base64.b64decode(data["content"]).decode("utf-8")
            return {"content": content, "sha": data["sha"]}
        return None

    def update_file(
        self, path: str, content: str, message: str, branch: str, file_sha: str
    ) -> dict | None:
        """Update (or create) a file on a branch. Returns the API response or None.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        """
        import base64

        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
            "sha": file_sha,
        }
        try:
            return self._put(url, json=payload)
        except RuntimeError:
            logger.exception("Failed to update file %s on branch %s", path, branch)
            return None

    def create_file(self, path: str, content: str, message: str, branch: str) -> dict | None:
        """Create a new file on a branch. Returns the API response or None.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        """
        import base64

        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/contents/{path}"
        payload = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": branch,
        }
        try:
            return self._put(url, json=payload)
        except RuntimeError:
            logger.exception("Failed to create file %s on branch %s", path, branch)
            return None

    def create_pull_request(
        self, title: str, body: str, head: str, base: str = "dev", draft: bool = True
    ) -> dict | None:
        """Create a pull request. Draft by default for safety.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        Returns the API response dict or None on failure.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/pulls"
        payload = {
            "title": title,
            "body": body,
            "head": head,
            "base": base,
            "draft": draft,
        }
        try:
            return self._post(url, json=payload)
        except RuntimeError:
            logger.exception("Failed to create PR: %s", title)
            return None

    def find_open_issue_by_title(self, title: str, label: str) -> dict | None:
        """Return the first open issue with the given label whose title matches exactly.

        Uses list_open_issues_by_label so no extra API surface is needed.
        Returns None if no match or on error.
        """
        issues = self.list_open_issues_by_label(label)
        for issue in issues:
            if issue.get("title") == title:
                return issue
        return None

    def update_issue(self, number: int, body: str) -> dict | None:
        """Update the body of an existing issue. Returns the response dict or None on failure."""
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/issues/{number}"
        try:
            return self._patch(url, json={"body": body})
        except RuntimeError:
            logger.exception("Failed to update issue #%d", number)
            return None

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        """Create a new GitHub issue. Returns the response dict.

        Rate limit / retry / backoff: exponential backoff on HTTP 429.
        Raises RuntimeError if the API call fails after all retries.
        """
        url = f"{_GITHUB_API_BASE}/repos/{self._repo}/issues"
        payload = {"title": title, "body": body, "labels": labels}
        return self._post(url, json=payload)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None) -> list | dict | None:
        backoff = 1.0
        for attempt in range(3):
            try:
                resp = httpx.get(
                    url, headers=self._headers, params=params, timeout=15.0, follow_redirects=True
                )
                if resp.status_code == 429:
                    if attempt == 2:
                        logger.error("GitHub GET rate-limited after 3 attempts: %s", url)
                        return None
                    logger.warning(
                        "GitHub GET rate-limited; retrying in %.0f s (attempt %d/3)",
                        backoff,
                        attempt + 1,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                logger.warning("GitHub GET HTTP error %s: %s", exc.response.status_code, url)
                return None
            except httpx.RequestError as exc:
                logger.warning("GitHub GET request error: %s", exc)
                return None
        return None

    def _put(self, url: str, json: dict) -> dict:
        backoff = 1.0
        for attempt in range(3):
            try:
                resp = httpx.put(
                    url, headers=self._headers, json=json, timeout=15.0, follow_redirects=True
                )
                if resp.status_code == 429:
                    if attempt == 2:
                        raise RuntimeError(f"GitHub PUT rate-limited after 3 attempts: {url}")
                    logger.warning(
                        "GitHub PUT rate-limited; retrying in %.0f s (attempt %d/3)",
                        backoff,
                        attempt + 1,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"GitHub PUT failed with HTTP {exc.response.status_code}"
                ) from exc
        raise RuntimeError("GitHub PUT: unreachable after retry loop")

    def _patch(self, url: str, json: dict) -> dict:
        backoff = 1.0
        for attempt in range(3):
            try:
                resp = httpx.patch(
                    url, headers=self._headers, json=json, timeout=15.0, follow_redirects=True
                )
                if resp.status_code == 429:
                    if attempt == 2:
                        raise RuntimeError(f"GitHub PATCH rate-limited after 3 attempts: {url}")
                    logger.warning(
                        "GitHub PATCH rate-limited; retrying in %.0f s (attempt %d/3)",
                        backoff,
                        attempt + 1,
                    )
                    import time

                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"GitHub PATCH failed with HTTP {exc.response.status_code}"
                ) from exc
        raise RuntimeError("GitHub PATCH: unreachable after retry loop")

    def _post(self, url: str, json: dict) -> dict:
        backoff = 1.0
        for attempt in range(3):
            try:
                resp = httpx.post(
                    url, headers=self._headers, json=json, timeout=15.0, follow_redirects=True
                )
                if resp.status_code == 429:
                    if attempt == 2:
                        raise RuntimeError(f"GitHub POST rate-limited after 3 attempts: {url}")
                    logger.warning(
                        "GitHub POST rate-limited; retrying in %.0f s (attempt %d/3)",
                        backoff,
                        attempt + 1,
                    )
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(
                    f"GitHub POST failed with HTTP {exc.response.status_code}"
                ) from exc
        raise RuntimeError("GitHub POST: unreachable after retry loop")


def get_github_client() -> GitHubClient | None:
    """Return the cached GitHubClient singleton, or None if env vars are not set.

    Thread-safe via double-checked locking (matches orchestrator.py pattern).
    Returns None (rather than raising) so callers can degrade gracefully.
    """
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        token = os.environ.get("GITHUB_TOKEN", "")
        repo = os.environ.get("GITHUB_REPO", _DEFAULT_REPO)
        if not token:
            logger.debug("GITHUB_TOKEN not set; GitHub issue creation disabled")
            return None
        _client = GitHubClient(token=token, repo=repo)
    return _client


def reset_github_client() -> None:
    """Reset the singleton — used in tests. Not called in production code."""
    global _client
    with _client_lock:
        _client = None
