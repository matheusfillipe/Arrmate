"""Listenarr audiobook manager client.

Auth: ``X-Api-Key`` header, base ``/api/v1``. Responses are plain JSON, so the
shared :class:`BaseMediaClient` transport applies unchanged. The routes are
*arr-adjacent but not *arr-shaped: the library noun is ``library`` rather than a
media-specific one, there is no ``/command`` queue, and indexer search is a
first-class ``/search`` endpoint instead of a per-item trigger.
"""

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .base import BaseMediaClient

BookStatus = Literal["downloading", "no-file", "quality-mismatch", "quality-match"]


class _ListenarrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class SystemInfo(_ListenarrRecord):
    version: str
    operating_system: str | None = Field(default=None, alias="operatingSystem")
    runtime: str | None = None
    start_time: str | None = Field(default=None, alias="startTime")


class SystemReady(_ListenarrRecord):
    is_ready: bool = Field(alias="isReady")
    status: str
    database_connected: bool | None = Field(default=None, alias="databaseConnected")
    migrations_current: bool | None = Field(default=None, alias="migrationsCurrent")


class DownloadClientHealth(_ListenarrRecord):
    name: str
    status: str
    type: str | None = None


class ExternalApiHealth(_ListenarrRecord):
    name: str
    status: str
    enabled: bool | None = None


class DownloadClientsHealth(_ListenarrRecord):
    status: str
    connected: int
    total: int
    clients: list[DownloadClientHealth] = []


class ExternalApisHealth(_ListenarrRecord):
    status: str
    connected: int
    total: int
    apis: list[ExternalApiHealth] = []


class SystemHealth(_ListenarrRecord):
    status: str
    version: str | None = None
    download_clients: DownloadClientsHealth | None = Field(default=None, alias="downloadClients")
    external_apis: ExternalApisHealth | None = Field(default=None, alias="externalApis")


class LibraryBook(_ListenarrRecord):
    id: int
    title: str
    authors: list[str] = []
    narrators: list[str] = []
    asin: str | None = None
    series: str | None = None
    series_number: str | None = Field(default=None, alias="seriesNumber")
    monitored: bool = False
    wanted: bool | None = None
    status: BookStatus | None = None
    file_count: int | None = Field(default=None, alias="fileCount")
    quality_profile_id: int | None = Field(default=None, alias="qualityProfileId")
    base_path: str | None = Field(default=None, alias="basePath")


class AudibleMetadata(_ListenarrRecord):
    """One metadata search hit, which is also the ``metadata`` object an add request wraps."""

    asin: str | None = None
    title: str | None = None
    subtitle: str | None = None
    author: str | None = None
    narrator: str | None = None
    publisher: str | None = None
    language: str | None = None
    series: str | None = None
    series_number: str | None = Field(default=None, alias="seriesNumber")
    published_date: str | None = Field(default=None, alias="publishedDate")
    image_url: str | None = Field(default=None, alias="imageUrl")
    isbn: list[str] = []
    source: str | None = None


class Release(_ListenarrRecord):
    title: str
    download_reference: str | None = Field(default=None, alias="downloadReference")
    indexer: str | None = None
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    protocol: str | None = None
    age_hours: float | None = Field(default=None, alias="ageHours")


class _AddRequest(_ListenarrRecord):
    metadata: AudibleMetadata
    monitored: bool
    auto_search: bool = Field(serialization_alias="autoSearch")
    quality_profile_id: int | None = Field(default=None, serialization_alias="qualityProfileId")


class AddResult(_ListenarrRecord):
    message: str | None = None
    audiobook: LibraryBook | None = None


class _GrabRequest(_ListenarrRecord):
    download_reference: str = Field(serialization_alias="downloadReference")
    download_client_id: str | None = Field(default=None, serialization_alias="downloadClientId")
    audiobook_id: int | None = Field(default=None, serialization_alias="audiobookId")


class GrabResult(_ListenarrRecord):
    download_id: str | None = Field(default=None, alias="downloadId")
    message: str | None = None


class QueueItem(_ListenarrRecord):
    id: str
    title: str
    status: str
    progress: float | None = None
    size: int | None = None
    download_client: str | None = Field(default=None, alias="downloadClient")
    audiobook_id: int | None = Field(default=None, alias="audiobookId")
    error_message: str | None = Field(default=None, alias="errorMessage")


class DownloadRecord(_ListenarrRecord):
    id: str
    title: str
    status: str
    audiobook_id: int | None = Field(default=None, alias="audiobookId")
    progress: float | None = None
    total_size: int | None = Field(default=None, alias="totalSize")
    download_client_name: str | None = Field(default=None, alias="downloadClientName")
    started_at: str | None = Field(default=None, alias="startedAt")
    import_attempts: int | None = Field(default=None, alias="importAttempts")
    metadata: dict[str, str | int | float | bool | None] = {}


class ImportedIndexer(_ListenarrRecord):
    id: int
    name: str
    url: str | None = None
    implementation: str | None = None


class ProwlarrImport(_ListenarrRecord):
    added_count: int = Field(alias="addedCount")
    skipped_count: int = Field(alias="skippedCount")
    total: int
    indexers: list[ImportedIndexer] = []


class HistoryEntry(_ListenarrRecord):
    id: int
    event_type: str = Field(alias="eventType")
    timestamp: str
    audiobook_id: int | None = Field(default=None, alias="audiobookId")
    audiobook_title: str | None = Field(default=None, alias="audiobookTitle")
    source_title: str | None = Field(default=None, alias="sourceTitle")
    message: str | None = None
    source: str | None = None
    outcome: str | None = None
    download_id: str | None = Field(default=None, alias="downloadId")


class HistoryPage(_ListenarrRecord):
    history: list[HistoryEntry] = []
    total: int = 0


class Indexer(_ListenarrRecord):
    id: int
    name: str
    implementation: str | None = None
    url: str | None = None
    is_enabled: bool | None = Field(default=None, alias="isEnabled")
    priority: int | None = None
    enable_automatic_search: bool | None = Field(default=None, alias="enableAutomaticSearch")
    enable_interactive_search: bool | None = Field(default=None, alias="enableInteractiveSearch")


class RootFolder(_ListenarrRecord):
    id: int
    name: str
    path: str
    is_default: bool = Field(default=False, alias="isDefault")


class QualityProfile(_ListenarrRecord):
    id: int
    name: str
    is_default: bool = Field(default=False, alias="isDefault")


_BOOKS = TypeAdapter(list[LibraryBook])
_METADATA = TypeAdapter(list[AudibleMetadata])
_RELEASES = TypeAdapter(list[Release])
_QUEUE = TypeAdapter(list[QueueItem])
_DOWNLOADS = TypeAdapter(list[DownloadRecord])
_INDEXERS = TypeAdapter(list[Indexer])
_ROOT_FOLDERS = TypeAdapter(list[RootFolder])
_QUALITY_PROFILES = TypeAdapter(list[QualityProfile])


class ListenarrClient(BaseMediaClient):
    """Client for the Listenarr v1 API (Audiobooks)."""

    api_prefix = "api/v1"

    async def test_connection(self) -> bool:
        """True when the system info endpoint answers with valid JSON."""
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> SystemInfo:  # type: ignore[override]
        """Version and runtime info.

        ``/system/status`` is not a route here; an unknown path falls through to
        the SPA and returns HTML, so this has to be ``/system/info``.
        """
        return SystemInfo.model_validate(await self._get(f"{self.api_prefix}/system/info"))

    async def get_ready(self) -> SystemReady:
        """Readiness detail: database, migrations and filesystem state."""
        return SystemReady.model_validate(await self._get(f"{self.api_prefix}/system/ready"))

    async def get_health(self) -> SystemHealth:
        """Health of the configured indexers and download clients."""
        return SystemHealth.model_validate(await self._get(f"{self.api_prefix}/system/health"))

    async def get_all_items(self) -> list[LibraryBook]:
        """Every audiobook in the library."""
        return _BOOKS.validate_python(await self._get(f"{self.api_prefix}/library"))

    async def get_item(self, item_id: int) -> LibraryBook:  # type: ignore[override]
        """One audiobook by library ID."""
        return LibraryBook.model_validate(await self._get(f"{self.api_prefix}/library/{item_id}"))

    async def delete_item(self, item_id: int, delete_files: bool = False) -> bool:
        """Remove an audiobook, optionally deleting its files."""
        await self._delete(
            f"{self.api_prefix}/library/{item_id}?deleteFiles={str(delete_files).lower()}"
        )
        return True

    async def search(  # type: ignore[override]
        self, query: str, category: str | None = None, limit: int = 50
    ) -> list[Release]:
        """Search the configured indexers for releases.

        This hits indexers directly, unlike the *arr ``lookup`` endpoints which
        search a metadata catalogue, so it runs on the extended timeout: a search
        fanned out over every configured indexer routinely outlives the default.
        """
        params = {"query": query}
        if category:
            params["category"] = category
        results = await self._get_with_timeout(f"{self.api_prefix}/search", params=params)
        if isinstance(results, dict):
            results = results.get("indexerResults") or results.get("results") or []
        return _RELEASES.validate_python(results)[:limit]

    async def search_metadata(self, query: str, limit: int = 25) -> list[AudibleMetadata]:
        """Search Audible/metadata providers for books, for the add workflow."""
        results = await self._get_with_timeout(
            f"{self.api_prefix}/search/intelligent",
            params={"query": query, "returnLimit": limit},
        )
        return _METADATA.validate_python(results)

    async def add_book(
        self,
        metadata: AudibleMetadata,
        quality_profile_id: int | None = None,
        monitored: bool = True,
        auto_search: bool = False,
    ) -> AddResult:
        """Add a book to the library from one :meth:`search_metadata` result."""
        request = _AddRequest(
            metadata=metadata,
            monitored=monitored,
            auto_search=auto_search,
            quality_profile_id=quality_profile_id,
        )
        added = await self._post(
            f"{self.api_prefix}/library/add",
            data=request.model_dump(by_alias=True, exclude_none=True),
        )
        return AddResult.model_validate(added)

    async def grab_release(
        self,
        download_reference: str,
        download_client_id: str | None = None,
        audiobook_id: int | None = None,
    ) -> GrabResult:
        """Send one release to a download client.

        The release is identified by the ``downloadReference`` string carried on
        each search result, not by the result object itself.
        """
        request = _GrabRequest(
            download_reference=download_reference,
            download_client_id=download_client_id,
            audiobook_id=audiobook_id,
        )
        sent = await self._post(
            f"{self.api_prefix}/download/send",
            data=request.model_dump(by_alias=True, exclude_none=True),
        )
        return GrabResult.model_validate(sent)

    async def get_queue(self) -> list[QueueItem]:
        """Live queue as reported by the download clients themselves."""
        snapshot = await self._get(f"{self.api_prefix}/download/queue")
        if isinstance(snapshot, dict):
            snapshot = snapshot.get("items") or snapshot.get("downloads") or []
        return _QUEUE.validate_python(snapshot)

    async def get_download_records(self) -> list[DownloadRecord]:
        """Listenarr's own download records, including failed and imported ones."""
        records = await self._get(f"{self.api_prefix}/downloads")
        if isinstance(records, dict):
            records = records.get("downloads") or records.get("items") or []
        return _DOWNLOADS.validate_python(records)

    async def import_from_prowlarr(self, url: str, api_key: str) -> ProwlarrImport:
        """Import indexers from a Prowlarr instance.

        Only Prowlarr indexers carrying category 3000/3030 are imported; the rest
        are skipped rather than rejected.
        """
        result = await self._post(
            f"{self.api_prefix}/indexers/prowlarr/import",
            data={"url": url, "apiKey": api_key},
        )
        return ProwlarrImport.model_validate(result)

    async def get_history(self, limit: int = 50) -> HistoryPage:
        """Grab/import history, newest first."""
        history = await self._get(f"{self.api_prefix}/history", params={"limit": limit})
        return HistoryPage.model_validate(history)

    async def get_indexers(self) -> list[Indexer]:
        """Configured indexers."""
        return _INDEXERS.validate_python(await self._get(f"{self.api_prefix}/indexers"))

    async def get_root_folders(self) -> list[RootFolder]:
        """Configured root folders."""
        return _ROOT_FOLDERS.validate_python(await self._get(f"{self.api_prefix}/rootfolders"))

    async def get_quality_profiles(self) -> list[QualityProfile]:
        """Configured quality profiles."""
        profiles = await self._get(f"{self.api_prefix}/qualityprofile")
        return _QUALITY_PROFILES.validate_python(profiles)
