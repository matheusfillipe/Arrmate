"""qBittorrent Web API client."""

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


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

    async def _get(self, path: str, params: dict | None = None) -> Any:
        await self._ensure_logged_in()
        resp = await self.client.get(f"{self.base_url}{path}", params=params)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return resp.text

    async def _post(self, path: str, data: dict | None = None) -> Any:
        await self._ensure_logged_in()
        resp = await self.client.post(f"{self.base_url}{path}", data=data or {})
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return resp.text

    async def test_connection(self) -> bool:
        try:
            await self._get("/api/v2/app/version")
            return True
        except Exception:
            return False

    async def get_transfer_info(self) -> dict[str, Any]:
        """Get transfer speeds and totals."""
        return await self._get("/api/v2/transfer/info")

    async def get_torrents(self) -> list[dict[str, Any]]:
        """Get list of all torrents."""
        result = await self._get("/api/v2/torrents/info")
        return result if isinstance(result, list) else []

    async def pause_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._post("/api/v2/torrents/pause", {"hashes": torrent_hash})
            return True
        except Exception:
            return False

    async def resume_torrent(self, torrent_hash: str) -> bool:
        try:
            await self._post("/api/v2/torrents/resume", {"hashes": torrent_hash})
            return True
        except Exception:
            return False

    async def set_download_limit(self, limit_bps: int) -> bool:
        """Set global download speed limit in bytes/s (0 = unlimited)."""
        try:
            await self._post("/api/v2/transfer/downloadLimit", {"limit": limit_bps})
            return True
        except Exception:
            return False

    async def set_upload_limit(self, limit_bps: int) -> bool:
        """Set global upload speed limit in bytes/s (0 = unlimited)."""
        try:
            await self._post("/api/v2/transfer/uploadLimit", {"limit": limit_bps})
            return True
        except Exception:
            return False

    async def delete_torrent(self, torrent_hash: str, delete_files: bool = False) -> bool:
        try:
            await self._post(
                "/api/v2/torrents/delete",
                {
                    "hashes": torrent_hash,
                    "deleteFiles": "true" if delete_files else "false",
                },
            )
            return True
        except Exception:
            return False

    async def set_priority(self, torrent_hash: str, action: str) -> bool:
        """Adjust queue priority. action: 'top', 'bottom', 'increase', 'decrease'."""
        endpoints = {
            "top": "/api/v2/torrents/topPrio",
            "bottom": "/api/v2/torrents/bottomPrio",
            "increase": "/api/v2/torrents/increasePrio",
            "decrease": "/api/v2/torrents/decreasePrio",
        }
        try:
            ep = endpoints.get(action)
            if not ep:
                return False
            await self._post(ep, {"hashes": torrent_hash})
            return True
        except Exception:
            return False

    async def add_url(self, url: str, category: str = "", paused: bool = False) -> bool:
        """Add a torrent or magnet link by URL."""
        try:
            await self._post(
                "/api/v2/torrents/add",
                {
                    "urls": url,
                    "category": category,
                    "paused": "true" if paused else "false",
                },
            )
            return True
        except Exception:
            return False

    async def get_item_files(self, torrent_hash: str) -> list[dict[str, Any]]:
        """List the files inside a torrent.

        The single highest-value diagnostic call in the downloader layer: a
        single non-video file, a ``.exe``/``.lnk``/``.scr``/``.zipx``, or a
        size that does not match the release means a poisoned swarm.

        Args:
            torrent_hash: Torrent hash

        Returns:
            List of file dicts with name, size, and progress
        """
        result = await self._get("/api/v2/torrents/files", {"hash": torrent_hash})
        return result if isinstance(result, list) else []

    async def recheck_torrent(self, torrent_hash: str) -> bool:
        """Force a hash recheck of a torrent."""
        try:
            await self._post("/api/v2/torrents/recheck", {"hashes": torrent_hash})
            return True
        except Exception:
            return False

    async def reannounce_torrent(self, torrent_hash: str) -> bool:
        """Force a tracker reannounce of a torrent."""
        try:
            await self._post("/api/v2/torrents/reannounce", {"hashes": torrent_hash})
            return True
        except Exception:
            return False
