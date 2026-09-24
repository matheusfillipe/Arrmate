"""Shared base for the *arr family of API clients (Sonarr/Radarr/Lidarr/Readarr).

Every method here is endpoint-parameterized by the ``entity`` noun (series,
movie, artist, author) and ``api_prefix``; subclasses add only what is unique
to their service. A subclass names its library and lookup models as type
arguments, ``class SonarrClient(BaseArrClient[Series, SeriesLookup])``, and the
shared methods validate responses into those models.
"""

import types
import typing
from typing import Any, ClassVar, Literal, Protocol, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .base import BaseMediaClient

CoverType = Literal[
    "unknown",
    "poster",
    "banner",
    "fanart",
    "screenshot",
    "headshot",
    "clearlogo",
    "cover",
    "disc",
    "logo",
]
DownloadProtocol = Literal["unknown", "usenet", "torrent"]
CommandStatus = Literal[
    "queued", "started", "completed", "failed", "aborted", "cancelled", "orphaned"
]
QueueStatus = Literal[
    "unknown",
    "queued",
    "paused",
    "downloading",
    "completed",
    "failed",
    "warning",
    "delay",
    "downloadClientUnavailable",
    "fallback",
]
TrackedDownloadStatus = Literal["ok", "warning", "error"]
TrackedDownloadState = Literal[
    "downloading",
    "importBlocked",
    "importPending",
    "importing",
    "imported",
    "failedPending",
    "failed",
    "ignored",
]


class ArrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ArrItem(Protocol):
    """What every library entry (series, movie, artist, author) offers."""

    @property
    def id(self) -> int: ...

    @property
    def title(self) -> str: ...

    @property
    def tags(self) -> list[int]: ...


class SystemStatus(ArrRecord):
    version: str


class Image(ArrRecord):
    cover_type: CoverType = Field(alias="coverType")
    url: str | None = None
    remote_url: str | None = Field(default=None, alias="remoteUrl")


def remote_poster(images: list[Image]) -> str | None:
    """The poster's address on the metadata site, which a browser loads without an API key."""
    return next(
        (image.remote_url for image in images if image.cover_type == "poster" and image.remote_url),
        None,
    )


class QualityProfile(ArrRecord):
    id: int
    name: str


class RootFolder(ArrRecord):
    id: int
    path: str
    free_space: int | None = Field(default=None, alias="freeSpace")
    accessible: bool | None = None


class Tag(ArrRecord):
    id: int
    label: str


class Quality(ArrRecord):
    id: int
    name: str


class QualityModel(ArrRecord):
    quality: Quality


class Command(ArrRecord):
    id: int
    name: str
    status: CommandStatus


class Release(ArrRecord):
    """One result of an interactive indexer search."""

    guid: str
    indexer_id: int = Field(alias="indexerId")
    indexer: str
    title: str
    size: int
    protocol: DownloadProtocol
    quality: QualityModel
    approved: bool
    rejected: bool = False
    #: The service keeps rejected releases in the result; "Release is blocklisted" on a
    #: well-seeded release is how a poisoned swarm shows up, so callers keep these.
    rejections: list[str] = []
    seeders: int | None = None
    leechers: int | None = None
    age: int | None = None
    publish_date: str | None = Field(default=None, alias="publishDate")


class BlocklistItem(ArrRecord):
    id: int
    source_title: str = Field(alias="sourceTitle")
    date: str
    protocol: DownloadProtocol
    quality: QualityModel
    indexer: str | None = None
    message: str | None = None


class HistoryRecord(ArrRecord):
    id: int
    source_title: str = Field(alias="sourceTitle")
    date: str
    quality: QualityModel
    download_id: str | None = Field(default=None, alias="downloadId")
    #: Event-specific details; the keys differ per event type, and every value is a string.
    data: dict[str, str | None] = {}

    @property
    def message(self) -> str | None:
        return self.data.get("message")


class StatusMessage(ArrRecord):
    title: str | None = None
    messages: list[str] = []


class QueueRecord(ArrRecord):
    id: int
    title: str | None = None
    size: float = 0
    sizeleft: float = 0
    status: QueueStatus | None = None
    tracked_download_status: TrackedDownloadStatus | None = Field(
        default=None, alias="trackedDownloadStatus"
    )
    tracked_download_state: TrackedDownloadState | None = Field(
        default=None, alias="trackedDownloadState"
    )
    status_messages: list[StatusMessage] = Field(default=[], alias="statusMessages")
    error_message: str | None = Field(default=None, alias="errorMessage")
    download_id: str | None = Field(default=None, alias="downloadId")
    protocol: DownloadProtocol | None = None
    download_client: str | None = Field(default=None, alias="downloadClient")
    indexer: str | None = None
    output_path: str | None = Field(default=None, alias="outputPath")
    estimated_completion_time: str | None = Field(default=None, alias="estimatedCompletionTime")
    quality: QualityModel | None = None

    @property
    def progress_percent(self) -> int:
        return int((self.size - self.sizeleft) / self.size * 100) if self.size else 0


class Page[RecordT](ArrRecord):
    total_records: int = Field(alias="totalRecords")
    records: list[RecordT] = []


class BaseArrClient[ItemT: ArrItem, LookupT: BaseModel](BaseMediaClient):
    """Client for the common *arr HTTP API shape."""

    entity: ClassVar[str] = "series"
    api_prefix: ClassVar[str] = "api/v3"
    search_command: ClassVar[str] = "SeriesSearch"
    #: Filled from the type arguments of the subclass; a subclass without them gets raw JSON.
    _models: ClassVar[tuple[type[BaseModel], type[BaseModel]] | None] = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for base in types.get_original_bases(cls):
            if typing.get_origin(base) is BaseArrClient:
                item_model, lookup_model = typing.get_args(base)
                cls._models = (item_model, lookup_model)

    def _item(self, raw: Any) -> ItemT:
        if self._models is None:
            return cast(ItemT, raw)
        return cast(ItemT, self._models[0].model_validate(raw))

    def _lookup(self, raw: Any) -> LookupT:
        if self._models is None:
            return cast(LookupT, raw)
        return cast(LookupT, self._models[1].model_validate(raw))

    async def _command(self, name: str, arguments: dict[str, int | list[int]]) -> Command:
        raw = await self._post(f"{self.api_prefix}/command", data={"name": name, **arguments})
        return Command.model_validate(raw)

    async def test_connection(self) -> bool:
        """True when the system status endpoint answers with valid JSON."""
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> SystemStatus:
        return SystemStatus.model_validate(await self._get(f"{self.api_prefix}/system/status"))

    async def search(self, query: str) -> list[LookupT]:
        """Search for items via the lookup endpoint."""
        raw = await self._get(f"{self.api_prefix}/{self.entity}/lookup", params={"term": query})
        return [self._lookup(entry) for entry in raw]

    async def get_item(self, item_id: int) -> ItemT:
        return self._item(await self._get(f"{self.api_prefix}/{self.entity}/{item_id}"))

    async def get_all_items(self) -> list[ItemT]:
        raw = await self._get(f"{self.api_prefix}/{self.entity}")
        return [self._item(entry) for entry in raw]

    async def delete_item(self, item_id: int, delete_files: bool = False) -> None:
        await self._delete(
            f"{self.api_prefix}/{self.entity}/{item_id}?deleteFiles={str(delete_files).lower()}"
        )

    async def get_quality_profiles(self) -> list[QualityProfile]:
        raw = await self._get(f"{self.api_prefix}/qualityprofile")
        return TypeAdapter(list[QualityProfile]).validate_python(raw)

    async def get_root_folders(self) -> list[RootFolder]:
        raw = await self._get(f"{self.api_prefix}/rootfolder")
        return TypeAdapter(list[RootFolder]).validate_python(raw)

    async def trigger_item_search(self, item_id: int) -> Command:
        """Search for every missing release of an item."""
        return await self._command(self.search_command, {f"{self.entity}Id": item_id})

    async def get_tags(self) -> list[Tag]:
        raw = await self._get(f"{self.api_prefix}/tag")
        return TypeAdapter(list[Tag]).validate_python(raw)

    async def create_tag(self, label: str) -> Tag:
        return Tag.model_validate(await self._post(f"{self.api_prefix}/tag", data={"label": label}))

    async def delete_tag(self, tag_id: int) -> None:
        await self._delete(f"{self.api_prefix}/tag/{tag_id}")

    async def push_release(self, release: Release) -> None:
        """Grab a release found by interactive search.

        The service finds the release in its search cache by ``guid`` and ``indexerId``.
        """
        await self._post(
            f"{self.api_prefix}/release",
            data={"guid": release.guid, "indexerId": release.indexer_id},
        )

    async def get_blocklist(self, page_size: int = 50) -> Page[BlocklistItem]:
        """Blocklisted releases, newest first."""
        raw = await self._get(
            f"{self.api_prefix}/blocklist",
            params={"pageSize": page_size, "sortKey": "date", "sortDirection": "descending"},
        )
        return Page[BlocklistItem].model_validate(raw)
