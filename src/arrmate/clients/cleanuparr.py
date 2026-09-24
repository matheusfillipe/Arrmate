"""Cleanuparr client (reverse-engineered, experimental).

Cleanuparr has no published API. Routes verified by inspection on 2.10.3:
they live under ``/api/...`` (not ``/api/v1/...``), the UI is JWT-gated but
each row in its users.db carries a 64-hex api_key that works as an
``X-Api-Key`` header, and unknown paths return the Angular index.html with
HTTP 200, so response body shape must be validated, never the status code.
"""

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

logger = logging.getLogger(__name__)


class _CleanuparrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class DownloadClientHealth(_CleanuparrRecord):
    client_name: str = Field(alias="clientName")
    client_type_name: str | None = Field(default=None, alias="clientTypeName")
    is_healthy: bool = Field(alias="isHealthy")
    last_checked: str | None = Field(default=None, alias="lastChecked")
    error_message: str | None = Field(default=None, alias="errorMessage")


class Event(_CleanuparrRecord):
    id: str
    timestamp: str
    event_type: str = Field(alias="eventType")
    message: str | None = None
    severity: str | None = None
    item_title: str | None = Field(default=None, alias="itemTitle")
    item_hash: str | None = Field(default=None, alias="itemHash")
    strike_count: int | None = Field(default=None, alias="strikeCount")
    download_client_id: str | None = Field(default=None, alias="downloadClientId")
    failed_import_reasons: list[str] = Field(default=[], alias="failedImportReasons")
    delete_reason: str | None = Field(default=None, alias="deleteReason")


class _EventPage(_CleanuparrRecord):
    items: list[Event]


_HEALTH = TypeAdapter(dict[str, DownloadClientHealth])


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

    async def _get(self, path: str, params: Mapping[str, int] | None = None) -> Any:
        resp = await self.client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        data = resp.json()
        # A 200 carrying the SPA shell means the route does not exist.
        if isinstance(data, dict) and data.get("__wix") is not None:
            raise ValueError(f"Cleanuparr returned the SPA shell for {path}")
        return data

    async def test_connection(self) -> bool:
        try:
            await self.get_health()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_health(self) -> dict[str, DownloadClientHealth]:
        """Health of each download client, keyed by client id."""
        return _HEALTH.validate_python(await self._get("/api/health"))

    async def get_events(self, page_size: int = 50, page: int = 0) -> list[Event]:
        """Recent strike/block events, newest first.

        The events list is what turns an unexplained arr failure into a named
        cause: Cleanuparr striking a blocked extension reports to Sonarr as
        "Manually marked as failed".
        """
        data = await self._get("/api/events", params={"pageSize": page_size, "page": page})
        return _EventPage.model_validate(data).items
