"""Jellyfin media server client.

Auth: ``Authorization: MediaBrowser Token="..."`` header. The OpenAPI spec is
at https://api.jellyfin.org/openapi/jellyfin-openapi-stable.json — these
calls are the hand-picked subset the agent needs.
"""

import logging
from typing import Any, cast

import httpx

logger = logging.getLogger(__name__)


class JellyfinClient:
    """Client for the Jellyfin REST API."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f'MediaBrowser Token="{self.api_key}"'},
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
        return resp.json() if resp.text else None

    async def _post(self, path: str) -> Any:
        resp = await self.client.post(f"{self.base_url}{path}")
        resp.raise_for_status()
        return resp.json() if resp.text else None

    async def test_connection(self) -> bool:
        try:
            info = await self._get("/System/Info")
            return bool(info.get("Version"))
        except Exception:
            return False

    async def get_system_info(self) -> dict[str, Any]:
        """Server name and version."""
        return cast(dict[str, Any], await self._get("/System/Info"))

    async def get_libraries(self) -> list[dict[str, Any]]:
        """List media libraries (virtual folders)."""
        return cast(list[dict[str, Any]], await self._get("/Library/MediaFolders"))

    async def get_items(
        self,
        library_id: str = "",
        item_type: str = "",
        search_term: str = "",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Query items; the general-purpose library read.

        item_type: 'Movie', 'Series', 'Episode'. Empty library_id searches all.
        """
        params: dict[str, Any] = {
            "Recursive": "true",
            "Limit": limit,
            "Fields": "Path,UserData,Overview",
        }
        if library_id:
            params["ParentId"] = library_id
        if item_type:
            params["IncludeItemTypes"] = item_type
        if search_term:
            params["SearchTerm"] = search_term
        return cast(dict[str, Any], await self._get("/Items", params=params))

    async def get_continue_watching(self, user_id: str, limit: int = 20) -> dict[str, Any]:
        """Items the user started and did not finish."""
        return cast(
            dict[str, Any],
            await self._get(
                f"/Users/{user_id}/Items/Resume",
                params={"Limit": limit, "MediaTypes": "Video"},
            ),
        )

    async def get_user_views(self, user_id: str) -> dict[str, Any]:
        """Libraries visible to a user."""
        return cast(dict[str, Any], await self._get(f"/Users/{user_id}/Views"))

    async def get_users(self) -> list[dict[str, Any]]:
        """All users with IDs (needed for watch-history queries)."""
        return cast(list[dict[str, Any]], await self._get("/Users"))

    async def get_item_playback_info(self, item_id: str) -> dict[str, Any]:
        """Media streams for an item — answers 'is this the bad audio track'."""
        return cast(dict[str, Any], await self._get(f"/Items/{item_id}/PlaybackInfo"))

    async def trigger_library_scan(self) -> dict[str, Any]:
        """Scan all libraries for new files (run after an import)."""
        return cast(dict[str, Any], await self._post("/Library/Refresh"))
