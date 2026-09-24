"""Jellyfin media server client.

Auth: ``Authorization: MediaBrowser Token="..."`` header. The OpenAPI spec is
at https://api.jellyfin.org/openapi/jellyfin-openapi-stable.json — these
calls are the hand-picked subset the agent needs.
"""

import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter
from pydantic.alias_generators import to_pascal

logger = logging.getLogger(__name__)

JellyfinItemType = Literal["Movie", "Series", "Episode"]


class _JellyfinRecord(BaseModel):
    model_config = ConfigDict(alias_generator=to_pascal, populate_by_name=True, extra="ignore")


class JellyfinUserData(_JellyfinRecord):
    played: bool | None = None
    played_percentage: float | None = None
    playback_position_ticks: int | None = None


class JellyfinItem(_JellyfinRecord):
    id: str
    name: str
    type: str
    production_year: int | None = None
    series_name: str | None = None
    parent_index_number: int | None = None
    index_number: int | None = None
    collection_type: str | None = None
    path: str | None = None
    overview: str | None = None
    user_data: JellyfinUserData | None = None


class JellyfinItemPage(_JellyfinRecord):
    items: list[JellyfinItem]
    total_record_count: int


class JellyfinUser(_JellyfinRecord):
    id: str
    name: str


class JellyfinSystemInfo(_JellyfinRecord):
    server_name: str | None = None
    version: str


class JellyfinMediaStream(_JellyfinRecord):
    index: int
    type: str
    codec: str | None = None
    language: str | None = None
    display_title: str | None = None
    channels: int | None = None
    is_default: bool | None = None
    is_forced: bool | None = None


class JellyfinMediaSource(_JellyfinRecord):
    id: str
    path: str | None = None
    container: str | None = None
    size: int | None = None
    media_streams: list[JellyfinMediaStream] = []


class JellyfinPlaybackInfo(_JellyfinRecord):
    media_sources: list[JellyfinMediaSource] = []


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

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> Any:
        resp = await self.client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def test_connection(self) -> bool:
        try:
            return bool((await self.get_system_info()).version)
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_info(self) -> JellyfinSystemInfo:
        """Server name and version."""
        return JellyfinSystemInfo.model_validate(await self._get("/System/Info"))

    async def get_libraries(self) -> JellyfinItemPage:
        """List media libraries (virtual folders)."""
        return JellyfinItemPage.model_validate(await self._get("/Library/MediaFolders"))

    async def get_items(
        self,
        library_id: str = "",
        item_type: JellyfinItemType | None = None,
        search_term: str = "",
        limit: int = 50,
    ) -> JellyfinItemPage:
        """Query items; the general-purpose library read. Empty library_id searches all."""
        params: dict[str, str | int] = {
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
        return JellyfinItemPage.model_validate(await self._get("/Items", params=params))

    async def get_continue_watching(self, user_id: str, limit: int = 20) -> JellyfinItemPage:
        """Items the user started and did not finish."""
        data = await self._get(
            f"/Users/{user_id}/Items/Resume",
            params={"Limit": limit, "MediaTypes": "Video"},
        )
        return JellyfinItemPage.model_validate(data)

    async def get_user_views(self, user_id: str) -> JellyfinItemPage:
        """Libraries visible to a user."""
        return JellyfinItemPage.model_validate(await self._get(f"/Users/{user_id}/Views"))

    async def get_users(self) -> list[JellyfinUser]:
        """All users with IDs (needed for watch-history queries)."""
        return TypeAdapter(list[JellyfinUser]).validate_python(await self._get("/Users"))

    async def get_item_playback_info(self, item_id: str) -> JellyfinPlaybackInfo:
        """Media streams for an item — answers 'is this the bad audio track'."""
        data = await self._get(f"/Items/{item_id}/PlaybackInfo")
        return JellyfinPlaybackInfo.model_validate(data)

    async def trigger_library_scan(self) -> None:
        """Scan all libraries for new files (run after an import)."""
        resp = await self.client.post(f"{self.base_url}/Library/Refresh")
        resp.raise_for_status()
