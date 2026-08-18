"""Gamearr game library manager client.

Auth: ``X-Api-Key`` header, base ``/api/v1``. Every response is wrapped in
``{success, data, error, code}``.
"""

import logging
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)


class GamearrClient:
    """Client for the Gamearr v1 API."""

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

    async def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None, json: Any = None
    ) -> Any:
        resp = await self.client.request(
            method, f"{self.base_url}/api/v1{path}", params=params, json=json
        )
        resp.raise_for_status()
        envelope = resp.json()
        if not envelope.get("success"):
            raise ValueError(envelope.get("error") or "gamearr request failed")
        return envelope.get("data")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: Any = None) -> Any:
        return await self._request("POST", path, json=json)

    async def test_connection(self) -> bool:
        try:
            await self._get("/system/status")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_games(self, limit: int = 0, offset: int = 0, store: str = "") -> list[Any]:
        """List games in the library, optionally paginated and filtered by store."""
        params: dict[str, Any] = {}
        if limit:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        if store:
            params["store"] = store
        return cast(list[Any], await self._get("/games", params=params))

    async def get_game(self, game_id: int) -> dict[str, Any]:
        """Get full details for one library game."""
        return cast(dict[str, Any], await self._get(f"/games/{game_id}"))

    async def search_games(self, query: str) -> list[Any]:
        """IGDB metadata search, resolves a title to an igdbId for add_game."""
        return cast(list[Any], await self._get("/search/games", params={"q": query}))

    async def add_game(
        self,
        igdb_id: int,
        monitored: bool = True,
        store: str = "",
        library_id: int = 0,
        status: str = "",
        platform: str = "",
    ) -> dict[str, Any]:
        """Add a game to the library from an IGDB search result."""
        body: dict[str, Any] = {"igdbId": igdb_id, "monitored": monitored}
        if store:
            body["store"] = store
        if library_id:
            body["libraryId"] = library_id
        if status:
            body["status"] = status
        if platform:
            body["platform"] = platform
        return cast(dict[str, Any], await self._post("/games", json=body))

    async def search_releases(self, game_id: int) -> list[Any]:
        """Prowlarr indexer search for candidate releases of a library game."""
        return cast(list[Any], await self._get(f"/search/releases/{game_id}"))

    async def grab_release(self, game_id: int, release: dict[str, Any]) -> dict[str, Any]:
        """Push one specific release (from search_releases) to the download client."""
        return cast(
            dict[str, Any],
            await self._post("/search/grab", json={"gameId": game_id, "release": release}),
        )

    async def get_downloads(self, include_completed: bool = False) -> list[Any]:
        """Get the active download queue."""
        params = {"includeCompleted": "true"} if include_completed else None
        return cast(list[Any], await self._get("/downloads", params=params))

    async def get_libraries(self) -> list[Any]:
        """List configured game libraries (name, path, platform, priority)."""
        return cast(list[Any], await self._get("/libraries"))
