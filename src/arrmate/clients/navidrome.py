"""Navidrome client over its native REST API, plus the Subsonic scan endpoints."""

from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

_SUBSONIC_API_VERSION = "1.16.1"
_PAGE_END = 5000


class _NavidromeRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class _Session(_NavidromeRecord):
    token: str
    username: str
    subsonic_token: str = Field(alias="subsonicToken")
    subsonic_salt: str = Field(alias="subsonicSalt")


class Song(_NavidromeRecord):
    id: str
    title: str
    artist: str | None = None
    album: str | None = None


class Playlist(_NavidromeRecord):
    id: str
    name: str
    owner_name: str | None = Field(default=None, alias="ownerName")
    owner_id: str | None = Field(default=None, alias="ownerId")
    public: bool = False
    song_count: int = Field(default=0, alias="songCount")


class PlaylistTrack(_NavidromeRecord):
    media_file_id: str = Field(alias="mediaFileId")
    title: str | None = None


class _User(_NavidromeRecord):
    id: str
    user_name: str = Field(alias="userName")


class ScanStatus(_NavidromeRecord):
    scanning: bool = False
    count: int | None = None
    folder_count: int | None = Field(default=None, alias="folderCount")
    last_scan: datetime | None = Field(default=None, alias="lastScan")
    scan_type: str | None = Field(default=None, alias="scanType")


class _SubsonicError(_NavidromeRecord):
    code: int | None = None
    message: str | None = None


class _SubsonicReply(_NavidromeRecord):
    status: str
    server_version: str | None = Field(default=None, alias="serverVersion")
    error: _SubsonicError | None = None
    scan_status: ScanStatus | None = Field(default=None, alias="scanStatus")


class _SubsonicEnvelope(_NavidromeRecord):
    reply: _SubsonicReply = Field(alias="subsonic-response")


class _Created(_NavidromeRecord):
    id: str


class _Added(_NavidromeRecord):
    added: int = 0


_SONGS = TypeAdapter(list[Song])
_PLAYLISTS = TypeAdapter(list[Playlist])
_PLAYLIST_TRACKS = TypeAdapter(list[PlaylistTrack])
_USERS = TypeAdapter(list[_User])


class NavidromeClient:
    """Client that signs in with a username and password and carries the issued JWT."""

    def __init__(self, base_url: str, username: str, password: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._session: _Session | None = None

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

    async def _login(self) -> _Session:
        if self._session is not None:
            return self._session
        response = await self.client.post(
            f"{self.base_url}/auth/login",
            json={"username": self.username, "password": self.password},
        )
        response.raise_for_status()
        self._session = _Session.model_validate(response.json())
        return self._session

    async def _api(
        self,
        method: str,
        path: str,
        params: dict[str, str | int] | None = None,
        body: dict[str, str | bool | list[str]] | None = None,
    ) -> Any:
        session = await self._login()
        response = await self.client.request(
            method,
            f"{self.base_url}/api/{path}",
            params=params,
            json=body,
            headers={"x-nd-authorization": f"Bearer {session.token}"},
        )
        response.raise_for_status()
        return response.json() if response.text else None

    async def _subsonic(self, endpoint: str) -> _SubsonicReply:
        session = await self._login()
        response = await self.client.get(
            f"{self.base_url}/rest/{endpoint}",
            params={
                "u": session.username,
                "t": session.subsonic_token,
                "s": session.subsonic_salt,
                "v": _SUBSONIC_API_VERSION,
                "c": "arrmate",
                "f": "json",
            },
        )
        response.raise_for_status()
        reply = _SubsonicEnvelope.model_validate(response.json()).reply
        if reply.status != "ok":
            raise ValueError(f"Navidrome refused {endpoint}: {reply.error}")
        return reply

    async def test_connection(self) -> bool:
        try:
            await self._login()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_version(self) -> str | None:
        return (await self._subsonic("ping")).server_version

    async def search_songs(self, title: str, limit: int = _PAGE_END) -> list[Song]:
        """Songs whose title contains the given text. A short common title ('Heaven') matches
        hundreds of songs and there is no artist filter, so the page has to hold them all."""
        songs = await self._api("GET", "song", params={"title": title, "_end": limit})
        return _SONGS.validate_python(songs)

    async def get_playlists(self) -> list[Playlist]:
        playlists = await self._api("GET", "playlist", params={"_end": _PAGE_END})
        return _PLAYLISTS.validate_python(playlists)

    async def get_playlist_tracks(self, playlist_id: str) -> list[PlaylistTrack]:
        tracks = await self._api(
            "GET", f"playlist/{playlist_id}/tracks", params={"_end": _PAGE_END}
        )
        return _PLAYLIST_TRACKS.validate_python(tracks)

    async def create_playlist(self, name: str, comment: str = "") -> str:
        """Create an empty playlist owned by the signed-in user and return its id."""
        created = await self._api("POST", "playlist", body={"name": name, "comment": comment})
        return _Created.model_validate(created).id

    async def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> int:
        added = await self._api("POST", f"playlist/{playlist_id}/tracks", body={"ids": song_ids})
        return _Added.model_validate(added).added

    async def update_playlist(
        self, playlist_id: str, name: str, public: bool, owner_id: str
    ) -> None:
        """Rename, set visibility and owner. Only an admin may hand a playlist to someone else."""
        await self._api(
            "PUT",
            f"playlist/{playlist_id}",
            body={"id": playlist_id, "name": name, "public": public, "ownerId": owner_id},
        )

    async def get_user_id(self, username: str) -> str:
        users = _USERS.validate_python(await self._api("GET", "user", params={"_end": _PAGE_END}))
        for user in users:
            if user.user_name == username:
                return user.id
        raise ValueError(f"no Navidrome user named {username!r}")

    async def start_scan(self) -> ScanStatus:
        """Pick up new files now rather than at the next scheduled scan."""
        return (await self._subsonic("startScan")).scan_status or ScanStatus()

    async def scan_status(self) -> ScanStatus:
        return (await self._subsonic("getScanStatus")).scan_status or ScanStatus()
