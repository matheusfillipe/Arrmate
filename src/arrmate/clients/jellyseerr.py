"""Jellyseerr/Overseerr request management client.

Auth: ``X-Api-Key`` header, base ``/api/v1``. OpenAPI spec:
https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml
"""

import logging
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)


class JellyseerrClient:
    """Client for the Jellyseerr v1 API."""

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
        resp = await self.client.get(f"{self.base_url}/api/v1{path}", params=params)
        resp.raise_for_status()
        return resp.json() if resp.text else None

    async def _post(self, path: str, json: Any = None) -> Any:
        resp = await self.client.post(f"{self.base_url}/api/v1{path}", json=json)
        resp.raise_for_status()
        return resp.json() if resp.text else None

    async def test_connection(self) -> bool:
        try:
            await self._get("/settings/status")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_requests(self, status: str = "", page_size: int = 50) -> dict[str, Any]:
        """List requests. status filter: 'pending', 'approved', 'declined', 'available'."""
        params: dict[str, Any] = {"take": page_size, "sort": "added"}
        if status:
            params["filter"] = status
        return cast(dict[str, Any], await self._get("/request", params=params))

    async def approve_request(self, request_id: int) -> Any:
        """Approve a pending request."""
        return cast(Any, await self._post(f"/request/{request_id}/approve"))

    async def decline_request(self, request_id: int) -> Any:
        """Decline a pending request."""
        return cast(Any, await self._post(f"/request/{request_id}/decline"))

    async def search_tmdb(self, query: str, page: int = 1) -> dict[str, Any]:
        """TMDB-backed search — resolves titles to tmdbIds without a TMDB key."""
        return cast(
            dict[str, Any], await self._get("/search", params={"query": query, "page": page})
        )

    async def get_tmdb_item(self, tmdb_id: int, media_type: str) -> dict[str, Any]:
        """Discover details for a tmdb item. media_type: 'movie' or 'tv'."""
        return cast(dict[str, Any], await self._get(f"/{media_type}/{tmdb_id}"))
