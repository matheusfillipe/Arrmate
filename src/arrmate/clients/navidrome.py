"""Navidrome client over its native REST API, plus the Subsonic scan endpoints."""

from typing import Any, cast

import httpx

_SUBSONIC_API_VERSION = "1.16.1"
_PAGE_END = 5000


class NavidromeClient:
    """Client that signs in with a username and password and carries the issued JWT."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session: dict[str, Any] | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
        self._session = None

    async def _login(self) -> dict[str, Any]:
        if self._session is not None:
            return self._session
        response = await self.client.post(
            f"{self.base_url}/auth/login",
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        session: dict[str, Any] = response.json()
        self._session = session
        return session

    async def _api(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        session = await self._login()
        response = await self.client.request(
            method,
            f"{self.base_url}/api/{path}",
            params=params,
            json=body,
            headers={"x-nd-authorization": f"Bearer {session['token']}"},
        )
        response.raise_for_status()
        return response.json() if response.text else None

    async def _subsonic(self, endpoint: str) -> dict[str, Any]:
        session = await self._login()
        response = await self.client.get(
            f"{self.base_url}/rest/{endpoint}",
            params={
                "u": session["username"],
                "t": session["subsonicToken"],
                "s": session["subsonicSalt"],
                "v": _SUBSONIC_API_VERSION,
                "c": "arrmate",
                "f": "json",
            },
        )
        response.raise_for_status()
        reply: dict[str, Any] = response.json()["subsonic-response"]
        if reply.get("status") != "ok":
            raise ValueError(f"Navidrome refused {endpoint}: {reply.get('error')}")
        return reply

    async def test_connection(self) -> bool:
        try:
            await self._login()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_version(self) -> str | None:
        version = (await self._subsonic("ping")).get("serverVersion")
        return str(version) if version else None

    async def search_songs(self, title: str, limit: int = _PAGE_END) -> list[dict[str, Any]]:
        """Songs whose title contains the given text. A short common title ('Heaven') matches
        hundreds of songs and there is no artist filter, so the page has to hold them all."""
        songs = await self._api("GET", "song", params={"title": title, "_end": limit})
        return cast("list[dict[str, Any]]", songs)

    async def get_playlists(self) -> list[dict[str, Any]]:
        playlists = await self._api("GET", "playlist", params={"_end": _PAGE_END})
        return cast("list[dict[str, Any]]", playlists)

    async def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        """Entries of a playlist; each carries the song id as ``mediaFileId``."""
        tracks = await self._api(
            "GET", f"playlist/{playlist_id}/tracks", params={"_end": _PAGE_END}
        )
        return cast("list[dict[str, Any]]", tracks)

    async def create_playlist(self, name: str, comment: str = "") -> str:
        """Create an empty playlist owned by the signed-in user and return its id."""
        created = await self._api("POST", "playlist", body={"name": name, "comment": comment})
        return str(created["id"])

    async def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> int:
        added = await self._api("POST", f"playlist/{playlist_id}/tracks", body={"ids": song_ids})
        return int(added.get("added", 0))

    async def update_playlist(
        self, playlist_id: str, name: str, public: bool, owner_id: str
    ) -> dict[str, Any]:
        """Rename, set visibility and owner. Only an admin may hand a playlist to someone else."""
        updated = await self._api(
            "PUT",
            f"playlist/{playlist_id}",
            body={"id": playlist_id, "name": name, "public": public, "ownerId": owner_id},
        )
        return cast("dict[str, Any]", updated)

    async def get_user_id(self, username: str) -> str:
        users = await self._api("GET", "user", params={"_end": _PAGE_END})
        for user in users:
            if user.get("userName") == username:
                return str(user["id"])
        raise ValueError(f"no Navidrome user named {username!r}")

    async def start_scan(self) -> dict[str, Any]:
        """Pick up new files now rather than at the next scheduled scan."""
        return cast("dict[str, Any]", (await self._subsonic("startScan")).get("scanStatus", {}))

    async def scan_status(self) -> dict[str, Any]:
        return cast("dict[str, Any]", (await self._subsonic("getScanStatus")).get("scanStatus", {}))
