"""Gamearr game library manager client.

Auth: ``X-Api-Key`` header, base ``/api/v1``. Every response is wrapped in
``{success, data, error, code}``.
"""

import logging
from collections.abc import Mapping
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

logger = logging.getLogger(__name__)

GameStatus = Literal["wanted", "downloading", "downloaded"]
NpsPlatform = Literal["PSV", "PSP", "PS3", "PSX", "PSM"]
NpsKind = Literal["GAMES", "DLCS", "UPDATES", "DEMOS"]
ReleaseProtocol = Literal["torrent", "usenet", "direct"]


class _GamearrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SystemStatus(_GamearrRecord):
    status: str
    version: str | None = None


class Game(_GamearrRecord):
    id: int
    igdb_id: int = Field(alias="igdbId")
    title: str
    year: int | None = None
    platform: str | None = None
    store: str | None = None
    status: GameStatus
    monitored: bool
    library_id: int | None = Field(default=None, alias="libraryId")
    folder_path: str | None = Field(default=None, alias="folderPath")
    installed_version: str | None = Field(default=None, alias="installedVersion")
    latest_version: str | None = Field(default=None, alias="latestVersion")
    update_available: bool = Field(default=False, alias="updateAvailable")


class GameSearchResult(_GamearrRecord):
    igdb_id: int = Field(alias="igdbId")
    title: str
    year: int | None = None
    platforms: list[str] = []
    developer: str | None = None
    publisher: str | None = None
    existing_game_id: int | None = Field(default=None, alias="existingGameId")
    steam_app_id: int | None = Field(default=None, alias="steamAppId")


class GameRelease(_GamearrRecord):
    """One indexer or catalogue release; grab_release sends it back as gamearr sent it."""

    guid: str | None = None
    title: str
    indexer: str | None = None
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    download_url: str | None = Field(default=None, alias="downloadUrl")
    magnet_url: str | None = Field(default=None, alias="magnetUrl")
    info_url: str | None = Field(default=None, alias="infoUrl")
    published_at: str | None = Field(default=None, alias="publishedAt")
    categories: list[int] | None = None
    protocol: ReleaseProtocol | None = None
    score: int | None = None


class GrabResult(_GamearrRecord):
    release_id: int | None = Field(default=None, alias="releaseId")
    torrent_hash: str | None = Field(default=None, alias="torrentHash")


class GameDownload(_GamearrRecord):
    hash: str
    name: str
    size: int | None = None
    progress: float
    download_speed: int | None = Field(default=None, alias="downloadSpeed")
    eta: int | None = None
    state: str
    category: str | None = None
    game_id: int | None = Field(default=None, alias="gameId")
    client: str | None = None


class Library(_GamearrRecord):
    id: int
    name: str
    path: str
    platform: str | None = None
    monitored: bool | None = None
    priority: int | None = None


class NpsEntry(_GamearrRecord):
    title_id: str = Field(alias="titleId")
    name: str
    region: str | None = None
    size: int | None = None
    platform: NpsPlatform
    kind: NpsKind


class NpsJob(_GamearrRecord):
    id: str
    title_id: str = Field(alias="titleId")
    name: str
    platform: NpsPlatform
    kind: NpsKind
    size: int | None = None
    received: int = 0
    state: Literal["downloading", "done", "failed"]
    error: str | None = None
    path: str | None = None
    zrif_path: str | None = Field(default=None, alias="zrifPath")


_GAMES = TypeAdapter(list[Game])
_SEARCH_RESULTS = TypeAdapter(list[GameSearchResult])
_RELEASES = TypeAdapter(list[GameRelease])
_DOWNLOADS = TypeAdapter(list[GameDownload])
_LIBRARIES = TypeAdapter(list[Library])
_NPS_ENTRIES = TypeAdapter(list[NpsEntry])
_NPS_JOBS = TypeAdapter(list[NpsJob])


class GamearrClient:
    """Client for the Gamearr v1 API."""

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

    async def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, str | int] | None = None,
        json: Mapping[str, object] | None = None,
    ) -> Any:
        resp = await self.client.request(
            method, f"{self.base_url}/api/v1{path}", params=params, json=json
        )
        resp.raise_for_status()
        envelope = resp.json()
        if not envelope.get("success"):
            raise ValueError(envelope.get("error") or "gamearr request failed")
        return envelope.get("data")

    async def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: Mapping[str, object]) -> Any:
        return await self._request("POST", path, json=json)

    async def test_connection(self) -> bool:
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> SystemStatus:
        return SystemStatus.model_validate(await self._get("/system/status"))

    async def get_games(self, limit: int = 0, offset: int = 0, store: str = "") -> list[Game]:
        """List games in the library, optionally paginated and filtered by store."""
        params: dict[str, str | int] = {}
        if limit:
            params["limit"] = limit
        if offset:
            params["offset"] = offset
        if store:
            params["store"] = store
        return _GAMES.validate_python(await self._get("/games", params=params))

    async def get_game(self, game_id: int) -> Game:
        return Game.model_validate(await self._get(f"/games/{game_id}"))

    async def search_games(self, query: str) -> list[GameSearchResult]:
        """IGDB metadata search, resolves a title to an igdbId for add_game."""
        return _SEARCH_RESULTS.validate_python(
            await self._get("/search/games", params={"q": query})
        )

    async def add_game(
        self,
        igdb_id: int,
        monitored: bool = True,
        store: str = "",
        library_id: int = 0,
        status: GameStatus | None = None,
        platform: str = "",
    ) -> Game:
        """Add a game to the library from an IGDB search result."""
        body: dict[str, object] = {"igdbId": igdb_id, "monitored": monitored}
        if store:
            body["store"] = store
        if library_id:
            body["libraryId"] = library_id
        if status:
            body["status"] = status
        if platform:
            body["platform"] = platform
        return Game.model_validate(await self._post("/games", json=body))

    async def nps_search(
        self, query: str, platform: NpsPlatform = "PSV", kind: NpsKind = "GAMES", limit: int = 25
    ) -> list[NpsEntry]:
        """Search the NoPayStation catalogue, which is keyed by title id rather than name."""
        data = await self._get(
            "/nps/search", params={"q": query, "platform": platform, "kind": kind, "limit": limit}
        )
        return _NPS_ENTRIES.validate_python(data)

    async def nps_title(self, title_id: str, platform: NpsPlatform = "PSV") -> list[NpsEntry]:
        """Every NoPayStation row for one title id: the game plus any update and DLC."""
        data = await self._get(f"/nps/title/{title_id}", params={"platform": platform})
        return _NPS_ENTRIES.validate_python(data)

    async def nps_download(
        self, title_id: str, platform: NpsPlatform = "PSV", kind: NpsKind = "GAMES"
    ) -> NpsJob:
        """Download one PKG and its zRIF key. Downloads only; nothing is installed."""
        data = await self._post(
            "/nps/download", json={"titleId": title_id, "platform": platform, "kind": kind}
        )
        return NpsJob.model_validate(data)

    async def nps_downloads(self) -> list[NpsJob]:
        """Progress for NoPayStation downloads requested since the server started."""
        return _NPS_JOBS.validate_python(await self._get("/nps/downloads"))

    async def search_releases(self, game_id: int) -> list[GameRelease]:
        """Prowlarr indexer search for candidate releases of a library game."""
        return _RELEASES.validate_python(await self._get(f"/search/releases/{game_id}"))

    async def grab_release(self, game_id: int, release: GameRelease) -> GrabResult:
        """Push one specific release (from search_releases) to the download client."""
        body = {
            "gameId": game_id,
            "release": release.model_dump(mode="json", by_alias=True, exclude_none=True),
        }
        return GrabResult.model_validate(await self._post("/search/grab", json=body))

    async def get_downloads(self, include_completed: bool = False) -> list[GameDownload]:
        """Get the active download queue."""
        params = {"includeCompleted": "true"} if include_completed else None
        return _DOWNLOADS.validate_python(await self._get("/downloads", params=params))

    async def get_libraries(self) -> list[Library]:
        """List configured game libraries (name, path, platform, priority)."""
        return _LIBRARIES.validate_python(await self._get("/libraries"))
