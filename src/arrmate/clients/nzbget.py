"""NZBget JSON-RPC download manager client."""

import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

logger = logging.getLogger(__name__)

GroupStatus = Literal[
    "QUEUED",
    "PAUSED",
    "DOWNLOADING",
    "FETCHING",
    "PP_QUEUED",
    "LOADING_PARS",
    "VERIFYING_SOURCES",
    "REPAIRING",
    "VERIFYING_REPAIRED",
    "RENAMING",
    "UNPACKING",
    "MOVING",
    "POST_UNPACK_RENAMING",
    "POST_DOWNLOAD_RENAMING",
    "EXECUTING_SCRIPT",
    "PP_FINISHED",
]

_EditParam = str | int | bool | list[int] | list[str]


def _join_64(lo: int, hi: int) -> int:
    """NZBget splits 64-bit sizes and rates into two 32-bit fields."""
    return (hi << 32) + lo


class _NzbgetRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Status(_NzbgetRecord):
    download_rate_lo: int | None = Field(default=None, alias="DownloadRateLo")
    download_rate_hi: int = Field(default=0, alias="DownloadRateHi")
    legacy_download_rate: int = Field(default=0, alias="DownloadRate")
    download_limit: int | None = Field(default=None, alias="DownloadLimit")
    download_paused: bool = Field(default=False, alias="DownloadPaused")

    @property
    def download_rate(self) -> int:
        """Current download speed in bytes per second."""
        if self.download_rate_lo is None:
            return self.legacy_download_rate
        return _join_64(self.download_rate_lo, self.download_rate_hi)


class Group(_NzbgetRecord):
    nzb_id: int = Field(alias="NZBID")
    nzb_name: str = Field(alias="NZBName")
    status: GroupStatus = Field(alias="Status")
    category: str = Field(default="", alias="Category")
    file_size_mb: int = Field(default=0, alias="FileSizeMB")
    remaining_size_mb: int = Field(default=0, alias="RemainingSizeMB")
    max_priority: int | None = Field(default=None, alias="MaxPriority")

    @property
    def percent_done(self) -> int:
        if self.file_size_mb <= 0:
            return 0
        return int((self.file_size_mb - self.remaining_size_mb) / self.file_size_mb * 100)


class File(_NzbgetRecord):
    filename: str = Field(alias="Filename")
    file_size_lo: int = Field(default=0, alias="FileSizeLo")
    file_size_hi: int = Field(default=0, alias="FileSizeHi")
    paused: bool = Field(default=False, alias="Paused")

    @property
    def size(self) -> int:
        return _join_64(self.file_size_lo, self.file_size_hi)


_GROUPS = TypeAdapter(list[Group])
_FILES = TypeAdapter(list[File])


class NZBgetClient:
    """Client for the NZBget JSON-RPC API."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                auth=(self.username, self.password),
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _rpc(self, method: str, params: list[_EditParam] | None = None) -> Any:
        """Call one RPC method and return its ``result``."""
        url = f"{self.base_url}/jsonrpc"
        payload = {"method": method, "params": params or [], "id": 1}
        resp = await self.client.post(url, json=payload)
        resp.raise_for_status()
        return resp.json().get("result")

    async def _action(self, method: str, params: list[_EditParam] | None = None) -> bool:
        try:
            return await self._rpc(method, params) is True
        except (httpx.HTTPError, ValueError):
            return False

    async def test_connection(self) -> bool:
        try:
            return bool(await self._rpc("version"))
        except (httpx.HTTPError, ValueError):
            return False

    async def get_status(self) -> Status:
        """Get download status including speed and pause state."""
        return Status.model_validate(await self._rpc("status"))

    async def get_queue(self) -> list[Group]:
        """Get the download queue."""
        return _GROUPS.validate_python(await self._rpc("listgroups", [0]))

    async def pause(self) -> bool:
        return await self._action("pausedownload")

    async def resume(self) -> bool:
        return await self._action("resumedownload")

    async def set_speed_limit(self, kbps: int) -> bool:
        """Set download rate limit in KB/s (0 = unlimited)."""
        return await self._action("rate", [kbps])

    async def delete_item(self, nzb_id: int) -> bool:
        return await self._action("editqueue", ["GroupDelete", "", [nzb_id]])

    async def set_priority(self, nzb_id: int, priority: int) -> bool:
        """Set item priority: -50=low, 0=normal, 50=high, 900=forced."""
        return await self._action("editqueue", ["GroupSetPriority", str(priority), [nzb_id]])

    async def pause_item(self, nzb_id: int) -> bool:
        """Pause a single queue item."""
        return await self._action("editqueue", ["GroupPause", "", [nzb_id]])

    async def resume_item(self, nzb_id: int) -> bool:
        """Resume a single paused queue item."""
        return await self._action("editqueue", ["GroupResume", "", [nzb_id]])

    async def move_item(self, nzb_id: int, offset: int) -> bool:
        """Move item by offset: positive=up, negative=down."""
        return await self._action("editqueue", ["GroupMoveOffset", str(offset), [nzb_id]])

    async def add_url(self, url: str, priority: int = 0, category: str = "") -> bool:
        """Add an NZB by URL."""
        filename = url.split("/")[-1] or "download.nzb"
        return await self._action(
            "appendurl", [filename, category, priority, False, url, "", 0, "SCORE", []]
        )

    async def get_item_files(self, nzb_id: int) -> list[File]:
        """List the files inside an NZB group."""
        return _FILES.validate_python(await self._rpc("listfiles", [0, 0, nzb_id]) or [])
