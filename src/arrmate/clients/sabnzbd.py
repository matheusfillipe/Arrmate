"""SABnzbd download manager client."""

import logging
from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)


class _SabRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")


class QueueSlot(_SabRecord):
    nzo_id: str
    filename: str
    status: str
    percentage: int = 0
    size: str | None = None
    sizeleft: str | None = None
    timeleft: str | None = None
    priority: str | None = None
    cat: str | None = None


class Queue(_SabRecord):
    status: str
    paused: bool = False
    kbpersec: float = 0
    speedlimit_abs: str | None = None
    slots: list[QueueSlot] = []


class QueueFile(_SabRecord):
    filename: str
    bytes: float = 0
    status: str | None = None


class _QueueResponse(_SabRecord):
    queue: Queue


class _FilesResponse(_SabRecord):
    files: list[QueueFile] = []


class SABnzbdClient:
    """Client for the SABnzbd HTTP API."""

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

    def _api_url(self) -> str:
        url = self.base_url.rstrip("/")
        # Accept URLs that already include the path (e.g. http://host:8080/sabnzbd)
        if url.endswith("/api"):
            return url
        if url.endswith("/sabnzbd"):
            return f"{url}/api"
        # Default: bare host:port, SABnzbd Docker (linuxserver) uses /api at root
        return f"{url}/api"

    async def _get(self, mode: str, extra: Mapping[str, str | int] | None = None) -> Any:
        params: dict[str, str | int] = {"apikey": self.api_key, "output": "json", "mode": mode}
        if extra:
            params.update(extra)
        resp = await self.client.get(self._api_url(), params=params)
        resp.raise_for_status()
        return resp.json()

    async def _action(self, mode: str, extra: Mapping[str, str | int] | None = None) -> bool:
        try:
            await self._get(mode, extra)
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def test_connection(self) -> bool:
        try:
            data = await self._get("version")
            return bool(data)
        except (httpx.HTTPError, ValueError):
            return False

    async def get_queue(self) -> Queue:
        """Get the download queue with its speed and pause state."""
        return _QueueResponse.model_validate(await self._get("queue")).queue

    async def pause(self) -> bool:
        return await self._action("pause")

    async def resume(self) -> bool:
        return await self._action("resume")

    async def set_speed_limit(self, kbps: int) -> bool:
        """Set download speed limit in KB/s (0 = unlimited)."""
        value = f"{kbps}K" if kbps > 0 else "0"
        return await self._action(
            "config", {"section": "misc", "keyword": "bandwidth_limit", "value": value}
        )

    async def delete_item(self, nzo_id: str, delete_files: bool = False) -> bool:
        return await self._action(
            "queue", {"name": "delete", "value": nzo_id, "del_files": 1 if delete_files else 0}
        )

    async def set_priority(self, nzo_id: str, priority: int) -> bool:
        """Set item priority: -1=low, 0=normal, 1=high, 2=forced."""
        return await self._action("queue", {"name": "priority", "value": nzo_id, "extra": priority})

    async def move_item(self, nzo_id: str, new_slot: int) -> bool:
        """Move item to an absolute queue slot position."""
        return await self._action("queue", {"name": "move", "value": nzo_id, "extra": new_slot})

    async def pause_item(self, nzo_id: str) -> bool:
        """Pause a single queue item."""
        return await self._action("queue", {"name": "pause", "value": nzo_id})

    async def resume_item(self, nzo_id: str) -> bool:
        """Resume a single paused queue item."""
        return await self._action("queue", {"name": "resume", "value": nzo_id})

    async def add_url(self, url: str, priority: int = 0, category: str = "") -> bool:
        """Add an NZB by URL."""
        return await self._action("addurl", {"name": url, "priority": priority, "cat": category})

    async def get_item_files(self, nzo_id: str) -> list[QueueFile]:
        """List the files inside a queue job."""
        data = await self._get("get_files", {"value": nzo_id})
        return _FilesResponse.model_validate(data).files
