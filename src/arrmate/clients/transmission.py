"""Transmission RPC client."""

import logging
from collections.abc import Mapping, Sequence
from enum import IntEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_TORRENT_FIELDS = [
    "id",
    "name",
    "status",
    "rateDownload",
    "rateUpload",
    "percentDone",
    "totalSize",
    "error",
    "errorString",
]


_RpcArguments = Mapping[str, int | bool | str | Sequence[int | str]]


class TorrentStatus(IntEnum):
    STOPPED = 0
    CHECK_QUEUED = 1
    CHECKING = 2
    DOWNLOAD_QUEUED = 3
    DOWNLOADING = 4
    SEED_QUEUED = 5
    SEEDING = 6


class _TransmissionRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Session(_TransmissionRecord):
    version: str | None = None
    speed_limit_down: int | None = Field(default=None, alias="speed-limit-down")
    speed_limit_down_enabled: bool = Field(default=False, alias="speed-limit-down-enabled")
    speed_limit_up: int | None = Field(default=None, alias="speed-limit-up")
    speed_limit_up_enabled: bool = Field(default=False, alias="speed-limit-up-enabled")


class TorrentFile(_TransmissionRecord):
    name: str
    length: int
    bytes_completed: int | None = Field(default=None, alias="bytesCompleted")


class Torrent(_TransmissionRecord):
    id: int
    name: str
    status: TorrentStatus | None = None
    rate_download: int | None = Field(default=None, alias="rateDownload")
    rate_upload: int | None = Field(default=None, alias="rateUpload")
    percent_done: float = Field(default=0, alias="percentDone")
    total_size: int | None = Field(default=None, alias="totalSize")
    error: int | None = None
    error_string: str | None = Field(default=None, alias="errorString")
    files: list[TorrentFile] = []


class _TorrentList(_TransmissionRecord):
    torrents: list[Torrent]


class TransmissionClient:
    """Client for the Transmission RPC API."""

    def __init__(
        self,
        base_url: str,
        username: str = "",
        # empty default = unauthenticated local daemon
        password: str = "",  # nosec B107
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._session_id = ""
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            auth = (self.username, self.password) if self.username else None
            self._client = httpx.AsyncClient(auth=auth, timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _rpc(self, method: str, arguments: _RpcArguments | None = None) -> Any:
        """Call one RPC method and return its ``arguments``, raising when it did not succeed."""
        url = f"{self.base_url}/transmission/rpc"
        payload = {"method": method, "arguments": arguments or {}}
        headers = {"X-Transmission-Session-Id": self._session_id}

        resp = await self.client.post(url, json=payload, headers=headers)

        if resp.status_code == 409:
            # Fetch the CSRF session token and retry
            self._session_id = resp.headers.get("X-Transmission-Session-Id", "")
            headers["X-Transmission-Session-Id"] = self._session_id
            resp = await self.client.post(url, json=payload, headers=headers)

        resp.raise_for_status()
        body = resp.json()
        if body.get("result") != "success":
            raise ValueError(f"transmission {method} failed: {body.get('result')}")
        return body.get("arguments")

    async def _action(self, method: str, arguments: _RpcArguments) -> bool:
        try:
            await self._rpc(method, arguments)
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def test_connection(self) -> bool:
        try:
            await self._rpc("session-get")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_session(self) -> Session:
        """Get session info including speed limits."""
        return Session.model_validate(await self._rpc("session-get"))

    async def get_torrents(self) -> list[Torrent]:
        """Get all torrents with their key fields."""
        arguments = await self._rpc("torrent-get", {"fields": _TORRENT_FIELDS})
        return _TorrentList.model_validate(arguments).torrents

    async def pause_torrent(self, torrent_id: int) -> bool:
        return await self._action("torrent-stop", {"ids": [torrent_id]})

    async def resume_torrent(self, torrent_id: int) -> bool:
        return await self._action("torrent-start", {"ids": [torrent_id]})

    async def set_speed_limit_down(self, kbps: int) -> bool:
        """Set download speed limit in KB/s (0 = disable limit)."""
        return await self._action(
            "session-set", {"speed-limit-down": kbps, "speed-limit-down-enabled": kbps > 0}
        )

    async def set_speed_limit_up(self, kbps: int) -> bool:
        """Set upload speed limit in KB/s (0 = disable limit)."""
        return await self._action(
            "session-set", {"speed-limit-up": kbps, "speed-limit-up-enabled": kbps > 0}
        )

    async def delete_torrent(self, torrent_id: int, delete_local_data: bool = False) -> bool:
        return await self._action(
            "torrent-remove", {"ids": [torrent_id], "delete-local-data": delete_local_data}
        )

    async def set_bandwidth_priority(self, torrent_id: int, priority: int) -> bool:
        """Set per-torrent bandwidth priority: -1=low, 0=normal, 1=high."""
        return await self._action(
            "torrent-set", {"ids": [torrent_id], "bandwidthPriority": priority}
        )

    async def add_url(self, url: str, paused: bool = False) -> bool:
        """Add a torrent or magnet link by URL."""
        return await self._action("torrent-add", {"filename": url, "paused": paused})

    async def get_item_files(self, torrent_id: int) -> list[TorrentFile]:
        """List the files inside a torrent."""
        arguments = await self._rpc(
            "torrent-get", {"ids": [torrent_id], "fields": ["id", "name", "files"]}
        )
        return [f for t in _TorrentList.model_validate(arguments).torrents for f in t.files]
