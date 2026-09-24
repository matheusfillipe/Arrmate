"""qBittorrent Web API client."""

import logging
from collections.abc import Mapping
from typing import Literal, assert_never

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter

logger = logging.getLogger(__name__)

TorrentState = Literal[
    "error",
    "missingFiles",
    "uploading",
    "pausedUP",
    "stoppedUP",
    "queuedUP",
    "stalledUP",
    "checkingUP",
    "forcedUP",
    "allocating",
    "downloading",
    "metaDL",
    "forcedMetaDL",
    "pausedDL",
    "stoppedDL",
    "queuedDL",
    "stalledDL",
    "checkingDL",
    "forcedDL",
    "checkingResumeData",
    "moving",
    "unknown",
]
QueueMove = Literal["top", "bottom", "increase", "decrease"]


class _QbitRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TransferInfo(_QbitRecord):
    dl_info_speed: int
    up_info_speed: int
    dl_info_data: int | None = None
    up_info_data: int | None = None
    dl_rate_limit: int | None = None
    up_rate_limit: int | None = None
    connection_status: Literal["connected", "firewalled", "disconnected"] | None = None


class Torrent(_QbitRecord):
    hash: str
    name: str
    #: qBittorrent adds states between releases; one we do not know must not fail the whole list.
    state: TorrentState | str
    progress: float
    size: int
    total_size: int | None = None
    dlspeed: int = 0
    upspeed: int = 0
    num_seeds: int = 0
    num_leechs: int = 0
    eta: int | None = None
    category: str = ""
    tags: str = ""
    save_path: str | None = None
    content_path: str | None = None
    added_on: int | None = None


class TorrentFile(_QbitRecord):
    name: str
    size: int
    progress: float = 0
    priority: int | None = None


_TORRENTS = TypeAdapter(list[Torrent])
_FILES = TypeAdapter(list[TorrentFile])


def _priority_endpoint(move: QueueMove) -> str:
    match move:
        case "top":
            return "/api/v2/torrents/topPrio"
        case "bottom":
            return "/api/v2/torrents/bottomPrio"
        case "increase":
            return "/api/v2/torrents/increasePrio"
        case "decrease":
            return "/api/v2/torrents/decreasePrio"
        case _:
            assert_never(move)


class QBittorrentClient:
    """Client for the qBittorrent Web API v2."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._logged_in = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._logged_in = False

    async def _ensure_logged_in(self) -> None:
        if self._logged_in:
            return
        resp = await self.client.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
        )
        resp.raise_for_status()
        self._logged_in = True

    async def _get(self, path: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        await self._ensure_logged_in()
        resp = await self.client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        return resp

    async def _post(self, path: str, data: Mapping[str, str | int]) -> None:
        """Send a form action. qBittorrent answers these with plain text or an empty body."""
        await self._ensure_logged_in()
        resp = await self.client.post(f"{self.base_url}{path}", data=data)
        resp.raise_for_status()

    async def _action(self, path: str, data: Mapping[str, str | int]) -> bool:
        try:
            await self._post(path, data)
            return True
        except httpx.HTTPError:
            return False

    async def test_connection(self) -> bool:
        try:
            await self._get("/api/v2/app/version")
            return True
        except httpx.HTTPError:
            return False

    async def get_transfer_info(self) -> TransferInfo:
        """Get transfer speeds and totals."""
        resp = await self._get("/api/v2/transfer/info")
        return TransferInfo.model_validate_json(resp.content)

    async def get_torrents(self) -> list[Torrent]:
        """Get list of all torrents."""
        return _TORRENTS.validate_json((await self._get("/api/v2/torrents/info")).content)

    async def pause_torrent(self, torrent_hash: str) -> bool:
        return await self._action("/api/v2/torrents/pause", {"hashes": torrent_hash})

    async def resume_torrent(self, torrent_hash: str) -> bool:
        return await self._action("/api/v2/torrents/resume", {"hashes": torrent_hash})

    async def set_download_limit(self, limit_bps: int) -> bool:
        """Set global download speed limit in bytes/s (0 = unlimited)."""
        return await self._action("/api/v2/transfer/downloadLimit", {"limit": limit_bps})

    async def set_upload_limit(self, limit_bps: int) -> bool:
        """Set global upload speed limit in bytes/s (0 = unlimited)."""
        return await self._action("/api/v2/transfer/uploadLimit", {"limit": limit_bps})

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> bool:
        return await self._action(
            "/api/v2/torrents/delete",
            {"hashes": torrent_hash, "deleteFiles": "true" if delete_files else "false"},
        )

    async def set_priority(self, torrent_hash: str, move: QueueMove) -> bool:
        """Move a torrent in the download queue."""
        return await self._action(_priority_endpoint(move), {"hashes": torrent_hash})

    async def add_url(self, url: str, category: str = "", paused: bool = False) -> bool:
        """Add a torrent or magnet link by URL."""
        return await self._action(
            "/api/v2/torrents/add",
            {"urls": url, "category": category, "paused": "true" if paused else "false"},
        )

    async def get_item_files(self, torrent_hash: str) -> list[TorrentFile]:
        """List the files inside a torrent.

        The single highest-value diagnostic call in the downloader layer: a
        single non-video file, a ``.exe``/``.lnk``/``.scr``/``.zipx``, or a
        size that does not match the release means a poisoned swarm.
        """
        resp = await self._get("/api/v2/torrents/files", {"hash": torrent_hash})
        return _FILES.validate_json(resp.content)

    async def recheck_torrent(self, torrent_hash: str) -> bool:
        """Force a hash recheck of a torrent."""
        return await self._action("/api/v2/torrents/recheck", {"hashes": torrent_hash})

    async def reannounce_torrent(self, torrent_hash: str) -> bool:
        """Force a tracker reannounce of a torrent."""
        return await self._action("/api/v2/torrents/reannounce", {"hashes": torrent_hash})
