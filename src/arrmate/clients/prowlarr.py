"""Prowlarr indexer aggregator client."""

import logging
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

logger = logging.getLogger(__name__)

DownloadProtocol = Literal["torrent", "usenet", "unknown"]


class _ProwlarrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SystemStatus(_ProwlarrRecord):
    app_name: str | None = Field(default=None, alias="appName")
    version: str


class Indexer(_ProwlarrRecord):
    id: int
    name: str
    enable: bool
    protocol: DownloadProtocol
    privacy: str | None = None
    priority: int | None = None


class Category(_ProwlarrRecord):
    id: int
    name: str | None = None


class Release(_ProwlarrRecord):
    guid: str
    title: str
    indexer_id: int = Field(alias="indexerId")
    indexer: str
    size: int = 0
    protocol: DownloadProtocol
    publish_date: str | None = Field(default=None, alias="publishDate")
    download_url: str | None = Field(default=None, alias="downloadUrl")
    magnet_url: str | None = Field(default=None, alias="magnetUrl")
    info_url: str | None = Field(default=None, alias="infoUrl")
    info_hash: str | None = Field(default=None, alias="infoHash")
    seeders: int | None = None
    leechers: int | None = None
    grabs: int | None = None
    categories: list[Category] = []

    @property
    def link(self) -> str:
        """The URL a download client can fetch, or the guid when the indexer gave neither."""
        return self.download_url or self.magnet_url or self.guid


class IndexerStat(_ProwlarrRecord):
    indexer_id: int = Field(alias="indexerId")
    indexer_name: str = Field(alias="indexerName")
    average_response_time: int | None = Field(default=None, alias="averageResponseTime")
    number_of_queries: int | None = Field(default=None, alias="numberOfQueries")
    number_of_grabs: int | None = Field(default=None, alias="numberOfGrabs")
    number_of_rss_queries: int | None = Field(default=None, alias="numberOfRssQueries")
    number_of_auth_queries: int | None = Field(default=None, alias="numberOfAuthQueries")
    number_of_failed_queries: int | None = Field(default=None, alias="numberOfFailedQueries")
    number_of_failed_grabs: int | None = Field(default=None, alias="numberOfFailedGrabs")
    number_of_failed_rss_queries: int | None = Field(default=None, alias="numberOfFailedRssQueries")
    number_of_failed_auth_queries: int | None = Field(
        default=None, alias="numberOfFailedAuthQueries"
    )


class IndexerStats(_ProwlarrRecord):
    indexers: list[IndexerStat] = []


_INDEXERS = TypeAdapter(list[Indexer])
_RELEASES = TypeAdapter(list[Release])


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

    async def _get(
        self, path: str, params: Mapping[str, str | int | Sequence[int]] | None = None
    ) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        resp = await self.client.get(url, headers=self._headers(), params=params)
        resp.raise_for_status()
        return resp.json()

    async def test_connection(self) -> bool:
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> SystemStatus:
        return SystemStatus.model_validate(await self._get("api/v1/system/status"))

    async def get_indexers(self) -> list[Indexer]:
        """Return all configured indexers."""
        return _INDEXERS.validate_python(await self._get("api/v1/indexer"))

    async def search(
        self, query: str, categories: list[int] | None = None, limit: int = 100
    ) -> list[Release]:
        """Search all indexers. categories is a list of numeric Prowlarr category IDs."""
        params: dict[str, str | int | Sequence[int]] = {
            "query": query,
            "type": "search",
            "limit": limit,
        }
        if categories:
            params["categories"] = categories
        return _RELEASES.validate_python(await self._get("api/v1/search", params))

    async def get_indexer_stats(self) -> IndexerStats:
        return IndexerStats.model_validate(await self._get("api/v1/indexerstats"))
