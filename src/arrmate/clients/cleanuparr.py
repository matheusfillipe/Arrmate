"""Cleanuparr client (reverse-engineered, experimental).

Cleanuparr has no published API. Routes verified by inspection on 2.10.3:
they live under ``/api/...`` (not ``/api/v1/...``), the UI is JWT-gated but
each row in its users.db carries a 64-hex api_key that works as an
``X-Api-Key`` header, and unknown paths return the Angular index.html with
HTTP 200 — so response body shape must be validated, never the status code.
"""

import logging
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)


class CleanuparrClient:
    """Client for the reverse-engineered Cleanuparr API."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = await self.client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
        # A 200 carrying the SPA shell means the route does not exist.
        if isinstance(data, dict) and data.get("__wix") is not None:
            raise ValueError(f"Cleanuparr returned the SPA shell for {path}")
        return data

    async def test_connection(self) -> bool:
        try:
            await self._get("/api/health")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_health(self) -> dict[str, Any]:
        """Per download-client health map."""
        return cast(dict[str, Any], await self._get("/api/health"))

    async def get_events(self, page_size: int = 50, page: int = 0) -> list[dict[str, Any]]:
        """Recent strike/block events, newest first (paged envelope).

        The events list is what turns an unexplained arr failure into a named
        cause: Cleanuparr striking a blocked extension reports to Sonarr as
        "Manually marked as failed".
        """
        data = await self._get("/api/events", params={"pageSize": page_size, "page": page})
        if isinstance(data, dict):
            return data.get("items") or data.get("results") or []
        return data if isinstance(data, list) else []
