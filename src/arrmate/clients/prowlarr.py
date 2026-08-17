"""Prowlarr indexer aggregator client."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ProwlarrClient:
    """Client for the Prowlarr API v1."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.api_key}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = await self.client.get(url, headers=self._headers(), params=params or {})
        resp.raise_for_status()
        return resp.json()

    async def test_connection(self) -> bool:
        try:
            data = await self._get("api/v1/system/status")
            return bool(data)
        except Exception:
            return False

    async def get_system_status(self) -> dict[str, Any]:
        return await self._get("api/v1/system/status")

    async def get_indexers(self) -> list[dict[str, Any]]:
        """Return all configured indexers."""
        return await self._get("api/v1/indexer")

    async def search(
        self, query: str, categories: list[int] | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Search all indexers. categories is a list of numeric Prowlarr category IDs."""
        params: dict[str, Any] = {"query": query, "type": "search", "limit": limit}
        if categories:
            params["categories"] = categories
        result = await self._get("api/v1/search", params)
        return result if isinstance(result, list) else []

    async def get_indexer_stats(self) -> dict[str, Any]:
        return await self._get("api/v1/indexerstats")
