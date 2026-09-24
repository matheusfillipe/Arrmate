"""ReadMeABook API client implementation.

ReadMeABook is a self-hosted audiobook library automation platform
that manages audiobook acquisition for Plex and AudioBookshelf.
It integrates with torrent/usenet download clients and provides
a request workflow for multi-user environments.

Default port: 3030
Auth: Bearer token (JWT from login or admin-generated API token)
"""

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .base_external import BaseExternalService

RequestStatus = Literal[
    "pending",
    "awaiting_approval",
    "denied",
    "searching",
    "downloading",
    "processing",
    "downloaded",
    "available",
    "failed",
    "cancelled",
    "awaiting_search",
    "awaiting_import",
    "awaiting_release",
    "warn",
]


class _RmabRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class AudibleBook(_RmabRecord):
    """An Audible catalogue entry as ReadMeABook serves it in search and discovery lists."""

    asin: str
    title: str
    author: str = ""
    narrator: str | None = None
    description: str | None = None
    cover_art_url: str | None = Field(default=None, alias="coverArtUrl")
    duration_minutes: int | None = Field(default=None, alias="durationMinutes")
    release_date: str | None = Field(default=None, alias="releaseDate")
    rating: float | None = None


class RequestedAudiobook(_RmabRecord):
    title: str
    author: str = ""
    audible_asin: str | None = Field(default=None, alias="audibleAsin")


class BookRequest(_RmabRecord):
    id: str
    status: RequestStatus
    type: Literal["audiobook", "ebook"] = "audiobook"
    progress: int | None = None
    error_message: str | None = Field(default=None, alias="errorMessage")
    audiobook: RequestedAudiobook | None = None

    def matches(self, asin: str, title: str) -> bool:
        """True when this request is for the book with this ASIN or title."""
        if self.audiobook is None:
            return False
        return self.audiobook.audible_asin == asin or (
            self.audiobook.title.lower() == title.lower()
        )


class _NewRequest(_RmabRecord):
    asin: str
    title: str
    author: str


class _NewRequestBody(_RmabRecord):
    audiobook: _NewRequest


class _SearchPage(_RmabRecord):
    results: list[AudibleBook] = []


class _Shelf(_RmabRecord):
    audiobooks: list[AudibleBook] = []


class _RequestsPage(_RmabRecord):
    requests: list[BookRequest] = []


class _Created(_RmabRecord):
    request: BookRequest


class _Version(_RmabRecord):
    version: str


class ReadMeABookClient(BaseExternalService):
    """Client for ReadMeABook REST API."""

    @property
    def client(self) -> httpx.AsyncClient:
        """HTTP client with Bearer token auth."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        return self._client

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _post(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.post(url, json=data or {})
        response.raise_for_status()
        return response.json()

    async def test_connection(self) -> bool:
        """Test connection to ReadMeABook."""
        try:
            await self._get("api/health")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get ReadMeABook library statistics."""
        try:
            data = await self._get("api/stats")
            if isinstance(data, dict):
                return data
            return {}
        except (httpx.HTTPError, ValueError):
            return {}

    async def get_version(self) -> str | None:
        try:
            return _Version.model_validate(await self._get("api/version")).version
        except (httpx.HTTPError, ValueError):
            return None

    async def search(self, query: str) -> list[AudibleBook]:
        """Search the Audible catalogue by title or author."""
        try:
            data = await self._get("api/audiobooks/search", params={"q": query})
            return _SearchPage.model_validate(data).results
        except (httpx.HTTPError, ValueError):
            return []

    async def get_popular(self) -> list[AudibleBook]:
        try:
            return _Shelf.model_validate(await self._get("api/audiobooks/popular")).audiobooks
        except (httpx.HTTPError, ValueError):
            return []

    async def get_new_releases(self) -> list[AudibleBook]:
        try:
            data = await self._get("api/audiobooks/new-releases")
            return _Shelf.model_validate(data).audiobooks
        except (httpx.HTTPError, ValueError):
            return []

    async def get_requests(self) -> list[BookRequest]:
        """The first page of requests visible to the token owner, newest first."""
        try:
            return _RequestsPage.model_validate(await self._get("api/requests")).requests
        except (httpx.HTTPError, ValueError):
            return []

    async def create_request(self, asin: str, title: str, author: str = "") -> BookRequest:
        body = _NewRequestBody(audiobook=_NewRequest(asin=asin, title=title, author=author))
        created = await self._post("api/requests", data=body.model_dump())
        return _Created.model_validate(created).request
