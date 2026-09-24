"""AudioBookshelf API client implementation.

AudioBookshelf is a self-hosted audiobook and podcast server with
a modern web UI, mobile apps, and robust playback tracking.
"""

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .base import BaseMediaClient

LibraryMediaType = Literal["book", "podcast"]


class _AbsRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class ServerStatus(_AbsRecord):
    version: str | None = Field(default=None, alias="serverVersion")
    is_init: bool | None = Field(default=None, alias="isInit")
    language: str | None = None


class Library(_AbsRecord):
    id: str
    name: str
    media_type: LibraryMediaType = Field(alias="mediaType")


class ItemMetadata(_AbsRecord):
    title: str | None = None
    subtitle: str | None = None
    author_name: str | None = Field(default=None, alias="authorName")
    narrator_name: str | None = Field(default=None, alias="narratorName")
    series_name: str | None = Field(default=None, alias="seriesName")
    published_year: str | None = Field(default=None, alias="publishedYear")
    asin: str | None = None
    isbn: str | None = None


class ItemMedia(_AbsRecord):
    metadata: ItemMetadata = ItemMetadata()
    duration: float | None = None
    num_tracks: int | None = Field(default=None, alias="numTracks")


class LibraryItem(_AbsRecord):
    id: str
    library_id: str | None = Field(default=None, alias="libraryId")
    media_type: LibraryMediaType | None = Field(default=None, alias="mediaType")
    path: str | None = None
    size: int | None = None
    media: ItemMedia = ItemMedia()


class LibraryItemsPage(_AbsRecord):
    results: list[LibraryItem] = []
    total: int = 0
    limit: int | None = None
    page: int | None = None


class _BookMatch(_AbsRecord):
    library_item: LibraryItem = Field(alias="libraryItem")


class _SearchResults(_AbsRecord):
    book: list[_BookMatch] = []


class MediaProgress(_AbsRecord):
    id: str
    library_item_id: str = Field(alias="libraryItemId")
    progress: float | None = None
    current_time: float | None = Field(default=None, alias="currentTime")
    duration: float | None = None
    is_finished: bool | None = Field(default=None, alias="isFinished")


class ListeningSession(_AbsRecord):
    id: str
    library_item_id: str | None = Field(default=None, alias="libraryItemId")
    display_title: str | None = Field(default=None, alias="displayTitle")
    display_author: str | None = Field(default=None, alias="displayAuthor")
    time_listening: float | None = Field(default=None, alias="timeListening")
    started_at: int | None = Field(default=None, alias="startedAt")
    updated_at: int | None = Field(default=None, alias="updatedAt")


class ListeningSessionsPage(_AbsRecord):
    sessions: list[ListeningSession] = []
    total: int = 0


class Series(_AbsRecord):
    id: str
    name: str
    books: list[LibraryItem] = []


class Collection(_AbsRecord):
    id: str
    name: str
    library_id: str | None = Field(default=None, alias="libraryId")
    books: list[LibraryItem] = []


class _CreateCollection(_AbsRecord):
    library_id: str = Field(serialization_alias="libraryId")
    name: str
    books: list[str]


class _ProgressUpdate(_AbsRecord):
    current_time: float = Field(serialization_alias="currentTime")
    is_finished: bool = Field(serialization_alias="isFinished")
    duration: float | None = None


class MatchResult(_AbsRecord):
    updated: bool | None = None
    warning: str | None = None


class _Libraries(_AbsRecord):
    libraries: list[Library] = []


class _MediaProgressList(_AbsRecord):
    media_progress: list[MediaProgress] = Field(default=[], alias="mediaProgress")


class _SeriesPage(_AbsRecord):
    results: list[Series] = []


class _Collections(_AbsRecord):
    collections: list[Collection] = []


class AudioBookshelfClient(BaseMediaClient):
    """Client for AudioBookshelf API (Audiobooks & Podcasts).

    AudioBookshelf is a purpose-built audiobook server with advanced
    playback features, progress tracking, and multi-user support.
    """

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with Bearer token auth."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
        return self._client

    async def _send(self, method: str, endpoint: str, body: _AbsRecord | None = None) -> None:
        """Send a write whose reply is a bare status, which the JSON helpers cannot parse."""
        response = await self.client.request(
            method,
            f"{self.base_url}/{endpoint}",
            json=body.model_dump(by_alias=True, exclude_none=True) if body else None,
        )
        response.raise_for_status()

    async def test_connection(self) -> bool:
        try:
            await self.get_libraries()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> ServerStatus:
        """Server version from the unauthenticated ``/status`` route."""
        return ServerStatus.model_validate(await self._get("status"))

    async def get_libraries(self) -> list[Library]:
        return _Libraries.model_validate(await self._get("api/libraries")).libraries

    async def _book_libraries(self) -> list[Library]:
        return [library for library in await self.get_libraries() if library.media_type == "book"]

    async def get_library(self, library_id: str) -> Library:
        return Library.model_validate(await self._get(f"api/libraries/{library_id}"))

    async def get_library_items(
        self,
        library_id: str,
        limit: int = 100,
        page: int = 0,
        sort: str | None = None,
        filter: str | None = None,
    ) -> LibraryItemsPage:
        """One page of a library; ``sort`` is a field path such as ``media.metadata.title``."""
        params: dict[str, int | str] = {"limit": limit, "page": page}
        if sort:
            params["sort"] = sort
        if filter:
            params["filter"] = filter
        items = await self._get(f"api/libraries/{library_id}/items", params=params)
        return LibraryItemsPage.model_validate(items)

    async def search(self, query: str) -> list[LibraryItem]:
        """Search every book library; AudioBookshelf only searches one library per call."""
        found = [
            _SearchResults.model_validate(
                await self._get(f"api/libraries/{library.id}/search", params={"q": query})
            )
            for library in await self._book_libraries()
        ]
        return [match.library_item for results in found for match in results.book]

    async def get_item(self, item_id: int | str) -> LibraryItem:
        return LibraryItem.model_validate(await self._get(f"api/items/{item_id}"))

    async def delete_item(self, item_id: int | str, delete_files: bool = False) -> bool:
        """Remove an item from the library; ``delete_files`` is ignored by AudioBookshelf."""
        await self._delete(f"api/items/{item_id}")
        return True

    async def get_progress(self) -> list[MediaProgress]:
        """The token owner's listening progress for every item."""
        progress = await self._get("api/me/progress")
        return _MediaProgressList.model_validate(progress).media_progress

    async def update_progress(
        self,
        item_id: str,
        current_time: float,
        duration: float | None = None,
        is_finished: bool = False,
    ) -> None:
        update = _ProgressUpdate(
            current_time=current_time, is_finished=is_finished, duration=duration
        )
        await self._send("PATCH", f"api/me/progress/{item_id}", update)

    async def get_sessions(self) -> ListeningSessionsPage:
        sessions = await self._get("api/me/listening-sessions")
        return ListeningSessionsPage.model_validate(sessions)

    async def get_series(self, library_id: str) -> list[Series]:
        return _SeriesPage.model_validate(
            await self._get(f"api/libraries/{library_id}/series")
        ).results

    async def get_collections(self, library_id: str) -> list[Collection]:
        return _Collections.model_validate(
            await self._get(f"api/libraries/{library_id}/collections")
        ).collections

    async def create_collection(
        self, library_id: str, name: str, book_ids: list[str]
    ) -> Collection:
        request = _CreateCollection(library_id=library_id, name=name, books=book_ids)
        created = await self._post(
            "api/collections", data=request.model_dump(by_alias=True, exclude_none=True)
        )
        return Collection.model_validate(created)

    async def scan_library(self, library_id: str) -> None:
        await self._send("POST", f"api/libraries/{library_id}/scan")

    async def match_audiobook(self, item_id: str) -> MatchResult:
        """Quick-match an item against the library's metadata provider."""
        return MatchResult.model_validate(await self._post(f"api/items/{item_id}/match"))

    async def get_all_books(self) -> list[LibraryItem]:
        """Every item across the book libraries."""
        pages = [
            await self.get_library_items(library.id, limit=1000)
            for library in await self._book_libraries()
        ]
        return [item for page in pages for item in page.results]
