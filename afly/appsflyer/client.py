"""Thin HTTP wrapper around AppsFlyer's REST APIs.

Deliberately does *not* interpret response status codes — that's
``afly.appsflyer.errors.classify_response``'s job, applied by the caller
(``pull_api``/``mng_api``) once it knows which endpoint's error semantics
apply. This class only owns: auth headers, timeouts, redirect-following, and
turning a network-level failure (not an HTTP error response) into a
:class:`TransientError` the retry policy already knows how to handle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import requests

from afly import __version__
from afly.appsflyer.errors import TransientError

_DEFAULT_BASE_URL = "https://hq1.appsflyer.com"


class AppsFlyerClient:
    """Authenticated HTTP client for one AppsFlyer account (one token)."""

    def __init__(
        self,
        token: str,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: int = 120,
        user_agent: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.user_agent = user_agent or f"afly/{__version__}"
        self._session = session or requests.Session()
        self.api_calls = 0

    def reset_counter(self) -> None:
        """Zero the ``api_calls`` counter (e.g. between extracts in a run)."""
        self.api_calls = 0

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        accept: str = "application/json",
    ) -> requests.Response:
        """GET ``base_url + path``, following redirects, with afly's standard headers.

        Increments ``api_calls`` exactly once per call — including a call
        that raises, since the network round-trip still happened and counts
        against AppsFlyer's rate limit either way.
        """
        url = f"{self.base_url}{path}"
        headers = {
            "authorization": f"Bearer {self._token}",
            "accept": accept,
            "user-agent": self.user_agent,
        }
        try:
            response = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=self.timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            self.api_calls += 1
            raise TransientError(
                f"network error calling AppsFlyer: {exc}", status=None, body=str(exc), url=url
            ) from exc

        self.api_calls += 1
        return response

    def __repr__(self) -> str:
        masked = f"{self._token[:4]}…" if self._token else "(empty)"
        return f"AppsFlyerClient(base_url={self.base_url!r}, token={masked!r})"
