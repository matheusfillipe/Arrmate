"""LazyLibrarian API client implementation.

LazyLibrarian is an automated book and audiobook manager similar to
Sonarr/Radarr, with support for NZB/torrent downloads, Goodreads/GoogleBooks
metadata, and Calibre integration.

Every call is ``GET /api?apikey=..&cmd=..``. Query commands answer with JSON
built from database rows, so their keys are the table's column names; action
commands answer with plain text such as ``OK`` or an error message.
"""

from typing import Any, Literal

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter

from .base import BaseMediaClient

BookType = Literal["eBook", "AudioBook"]


class _LazyLibrarianRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class VersionInfo(_LazyLibrarianRecord):
    success: bool = Field(alias="Success")
    version: str | None = Field(default=None, alias="current_version")
    latest_version: str | None = None
    install_type: str | None = None
    commits_behind: int | None = None


class Author(_LazyLibrarianRecord):
    author_id: str = Field(alias="AuthorID")
    author_name: str = Field(alias="AuthorName")
    status: str | None = Field(default=None, alias="Status")
    have_books: int | None = Field(default=None, alias="HaveBooks")
    total_books: int | None = Field(default=None, alias="TotalBooks")
    last_book: str | None = Field(default=None, alias="LastBook")
    date_added: str | None = Field(default=None, alias="DateAdded")


class Book(_LazyLibrarianRecord):
    book_id: str = Field(alias="BookID")
    book_name: str = Field(alias="BookName")
    author_id: str | None = Field(default=None, alias="AuthorID")
    author_name: str | None = Field(default=None, alias="AuthorName")
    book_sub: str | None = Field(default=None, alias="BookSub")
    book_isbn: str | None = Field(default=None, alias="BookIsbn")
    book_pub: str | None = Field(default=None, alias="BookPub")
    book_date: str | None = Field(default=None, alias="BookDate")
    book_lang: str | None = Field(default=None, alias="BookLang")
    book_img: str | None = Field(default=None, alias="BookImg")
    status: str | None = Field(default=None, alias="Status")
    audio_status: str | None = Field(
        default=None, validation_alias=AliasChoices("AudioStatus", "audiostatus", "audio_status")
    )


class AuthorDetail(_LazyLibrarianRecord):
    author: list[Author] = []
    books: list[Book] = []


class MetadataMatch(_LazyLibrarianRecord):
    """One hit from the Goodreads/GoogleBooks/OpenLibrary lookup behind findAuthor/findBook."""

    author_name: str | None = Field(default=None, alias="authorname")
    author_id: str | None = Field(default=None, alias="authorid")
    book_id: str | None = Field(default=None, alias="bookid")
    book_name: str | None = Field(default=None, alias="bookname")
    book_sub: str | None = Field(default=None, alias="booksub")
    book_isbn: str | None = Field(default=None, alias="bookisbn")
    book_date: str | None = Field(default=None, alias="bookdate")
    book_lang: str | None = Field(default=None, alias="booklang")
    source: str | None = None
    highest_fuzz: float | None = None


class SearchResult(_LazyLibrarianRecord):
    """One provider release from searchItem."""

    title: str
    provider: str
    score: float | None = None
    size: str | None = None
    date: str | None = None
    url: str | None = None
    mode: str | None = None


class Magazine(_LazyLibrarianRecord):
    title: str = Field(alias="Title")
    status: str | None = Field(default=None, alias="Status")
    issue_status: str | None = Field(default=None, alias="IssueStatus")
    issue_date: str | None = Field(default=None, alias="IssueDate")
    last_acquired: str | None = Field(default=None, alias="LastAcquired")


class Issue(_LazyLibrarianRecord):
    title: str = Field(alias="Title")
    issue_id: str = Field(alias="IssueID")
    issue_date: str | None = Field(default=None, alias="IssueDate")
    issue_acquired: str | None = Field(default=None, alias="IssueAcquired")
    issue_file: str | None = Field(default=None, alias="IssueFile")


class MagazineIssues(_LazyLibrarianRecord):
    magazine: list[Magazine] = []
    issues: list[Issue] = []


_AUTHORS = TypeAdapter(list[Author])
_BOOKS = TypeAdapter(list[Book])
_MATCHES = TypeAdapter(list[MetadataMatch])
_RESULTS = TypeAdapter(list[SearchResult])
_MAGAZINES = TypeAdapter(list[Magazine])


def _flags(**flags: bool) -> dict[str, str]:
    """LazyLibrarian reads a flag as set when its key is present at all."""
    return {name: "1" for name, enabled in flags.items() if enabled}


class LazyLibrarianClient(BaseMediaClient):
    """Client for LazyLibrarian API (Books & Audiobooks).

    LazyLibrarian provides automated downloading and management of
    books and audiobooks with Goodreads/GoogleBooks integration.
    """

    async def _call(self, cmd: str, params: dict[str, str] | None = None) -> httpx.Response:
        response = await self.client.get(
            f"{self.base_url}/api", params={"apikey": self.api_key, "cmd": cmd, **(params or {})}
        )
        response.raise_for_status()
        return response

    async def _query(self, cmd: str, params: dict[str, str] | None = None) -> Any:
        return (await self._call(cmd, params)).json()

    async def _command(self, cmd: str, params: dict[str, str] | None = None) -> str:
        """Run an action command and return LazyLibrarian's text reply, ``OK`` on success."""
        return (await self._call(cmd, params)).text

    async def test_connection(self) -> bool:
        try:
            return (await self.get_system_status()).success
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> VersionInfo:  # type: ignore[override]
        return VersionInfo.model_validate(await self._query("getVersion"))

    async def search(self, query: str) -> list[SearchResult]:  # type: ignore[override]
        """Search every enabled provider for releases matching a title or author."""
        return _RESULTS.validate_python(await self._query("searchItem", {"item": query}))

    async def find_author(self, name: str) -> list[MetadataMatch]:
        """Look an author up on the configured metadata source."""
        return _MATCHES.validate_python(await self._query("findAuthor", {"name": name}))

    async def find_book(self, name: str) -> list[MetadataMatch]:
        """Look a book up on the configured metadata source."""
        return _MATCHES.validate_python(await self._query("findBook", {"name": name}))

    async def add_author(self, name: str) -> str:
        return await self._command("addAuthor", {"name": name})

    async def add_author_by_id(self, author_id: str) -> str:
        return await self._command("addAuthorID", {"id": author_id})

    async def get_author(self, author_id: str) -> AuthorDetail:
        """Author row plus every book LazyLibrarian holds for them."""
        return AuthorDetail.model_validate(await self._query("getAuthor", {"id": author_id}))

    async def get_item(self, item_id: int) -> AuthorDetail:  # type: ignore[override]
        return await self.get_author(str(item_id))

    async def delete_item(self, item_id: int, delete_files: bool = False) -> bool:
        """Remove an author; LazyLibrarian never deletes files through the API."""
        return await self._command("removeAuthor", {"id": str(item_id)}) == "OK"

    async def pause_author(self, author_id: str) -> str:
        return await self._command("pauseAuthor", {"id": author_id})

    async def resume_author(self, author_id: str) -> str:
        return await self._command("resumeAuthor", {"id": author_id})

    async def refresh_author(self, name: str, refresh: bool = True) -> str:
        """Reload an author from the metadata source; ``refresh`` forces it even when recent."""
        return await self._command("refreshAuthor", {"name": name, **_flags(refresh=refresh)})

    async def get_all_books(self) -> list[Book]:
        return _BOOKS.validate_python(await self._query("getAllBooks"))

    async def get_all_authors(self) -> list[Author]:
        return _AUTHORS.validate_python(await self._query("getIndex"))

    async def add_book(self, book_id: str) -> str:
        return await self._command("addBook", {"id": book_id})

    async def queue_book(self, book_id: str, book_type: BookType = "eBook") -> str:
        """Mark a book as wanted."""
        return await self._command("queueBook", {"id": book_id, "type": book_type})

    async def unqueue_book(self, book_id: str, book_type: BookType = "eBook") -> str:
        """Mark a book as skipped."""
        return await self._command("unqueueBook", {"id": book_id, "type": book_type})

    async def search_book(
        self, book_id: str, book_type: BookType | None = None, wait: bool = False
    ) -> str:
        params = {"id": book_id, **_flags(wait=wait)}
        if book_type:
            params["type"] = book_type
        return await self._command("searchBook", params)

    async def force_library_scan(
        self, wait: bool = False, remove: bool = False, directory: str | None = None
    ) -> str:
        params = _flags(wait=wait, remove=remove)
        if directory:
            params["dir"] = directory
        return await self._command("forceLibraryScan", params)

    async def force_audiobook_scan(self, wait: bool = False) -> str:
        return await self._command("forceAudioBookScan", _flags(wait=wait))

    async def get_magazines(self) -> list[Magazine]:
        return _MAGAZINES.validate_python(await self._query("getMagazines"))

    async def add_magazine(self, name: str) -> str:
        return await self._command("addMagazine", {"name": name})

    async def get_issues(self, magazine_name: str) -> MagazineIssues:
        return MagazineIssues.model_validate(
            await self._query("getIssues", {"name": magazine_name})
        )

    async def restart(self) -> str:
        return await self._command("restart")

    async def shutdown(self) -> str:
        return await self._command("shutdown")
