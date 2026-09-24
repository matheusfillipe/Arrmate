"""Jellyseerr/Overseerr request management client.

Auth: ``X-Api-Key`` header, base ``/api/v1``. OpenAPI spec:
https://github.com/seerr-team/seerr/blob/develop/seerr-api.yml
"""

import logging
from enum import IntEnum
from typing import Annotated, Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer
from pydantic.alias_generators import to_camel

logger = logging.getLogger(__name__)

TitleType = Literal["movie", "tv"]
RequestFilter = Literal[
    "all",
    "approved",
    "available",
    "pending",
    "processing",
    "unavailable",
    "failed",
    "deleted",
    "completed",
]


class RequestStatus(IntEnum):
    PENDING = 1
    APPROVED = 2
    DECLINED = 3
    FAILED = 4
    COMPLETED = 5


class MediaStatus(IntEnum):
    UNKNOWN = 1
    PENDING = 2
    PROCESSING = 3
    PARTIALLY_AVAILABLE = 4
    AVAILABLE = 5
    BLACKLISTED = 6
    DELETED = 7


def _status_name(status: IntEnum) -> str:
    return status.name.lower()


RequestStatusName = Annotated[RequestStatus, PlainSerializer(_status_name)]
MediaStatusName = Annotated[MediaStatus, PlainSerializer(_status_name)]


class _SeerrRecord(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class JellyseerrUser(_SeerrRecord):
    id: int
    display_name: str | None = None


class JellyseerrMedia(_SeerrRecord):
    id: int
    media_type: TitleType
    tmdb_id: int | None = None
    tvdb_id: int | None = None
    status: MediaStatusName


class JellyseerrRequest(_SeerrRecord):
    id: int
    status: RequestStatusName
    type: TitleType
    is_4k: bool = Field(alias="is4k")
    created_at: str
    media: JellyseerrMedia
    requested_by: JellyseerrUser | None = None


class JellyseerrPageInfo(_SeerrRecord):
    page: int
    pages: int
    page_size: int
    results: int


class JellyseerrRequestPage(_SeerrRecord):
    page_info: JellyseerrPageInfo
    results: list[JellyseerrRequest]


class JellyseerrSearchResult(_SeerrRecord):
    id: int
    media_type: Literal["movie", "tv", "person"]
    title: str | None = None
    name: str | None = None
    release_date: str | None = None
    first_air_date: str | None = None
    overview: str | None = None


class JellyseerrSearchPage(_SeerrRecord):
    page: int
    total_pages: int
    total_results: int
    results: list[JellyseerrSearchResult]


class JellyseerrTitle(_SeerrRecord):
    id: int
    title: str | None = None
    name: str | None = None
    overview: str | None = None
    release_date: str | None = None
    first_air_date: str | None = None
    status: str | None = None
    media_info: JellyseerrMedia | None = None


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

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> Any:
        resp = await self.client.get(f"{self.base_url}/api/v1{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str) -> Any:
        resp = await self.client.post(f"{self.base_url}/api/v1{path}")
        resp.raise_for_status()
        return resp.json()

    async def test_connection(self) -> bool:
        try:
            await self._get("/status")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_requests(
        self, status: RequestFilter | None = None, page_size: int = 50
    ) -> JellyseerrRequestPage:
        """List requests, newest first, optionally filtered by status."""
        params: dict[str, str | int] = {"take": page_size, "sort": "added"}
        if status:
            params["filter"] = status
        return JellyseerrRequestPage.model_validate(await self._get("/request", params=params))

    async def approve_request(self, request_id: int) -> JellyseerrRequest:
        """Approve a pending request."""
        return JellyseerrRequest.model_validate(await self._post(f"/request/{request_id}/approve"))

    async def decline_request(self, request_id: int) -> JellyseerrRequest:
        """Decline a pending request."""
        return JellyseerrRequest.model_validate(await self._post(f"/request/{request_id}/decline"))

    async def search_tmdb(self, query: str, page: int = 1) -> JellyseerrSearchPage:
        """TMDB-backed search — resolves titles to tmdbIds without a TMDB key."""
        # Seerr rejects a query whose spaces arrive as '+', so we percent-encode it ourselves.
        data = await self._get(f"/search?query={quote(query, safe='')}&page={page}")
        return JellyseerrSearchPage.model_validate(data)

    async def get_tmdb_item(self, tmdb_id: int, media_type: TitleType) -> JellyseerrTitle:
        """Discover details for a tmdb item."""
        return JellyseerrTitle.model_validate(await self._get(f"/{media_type}/{tmdb_id}"))
