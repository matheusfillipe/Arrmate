"""Open Library API client for book discovery.

Open Library is free and requires no API key.
https://openlibrary.org/developers/api
"""

from typing import Literal

import httpx
from pydantic import BaseModel

COVER_BASE = "https://covers.openlibrary.org/b/id"


class BookCard(BaseModel):
    display_title: str
    author: str
    year: str
    poster: str | None
    overview: str
    ol_key: str
    media_type: Literal["book"] = "book"
    in_library: bool = False


class _TrendingWork(BaseModel):
    key: str = ""
    title: str = "Unknown"
    author_name: list[str] = []
    first_publish_year: int | None = None
    cover_i: int | None = None
    subject: list[str] = []


class _SubjectAuthor(BaseModel):
    name: str = ""


class _SubjectWork(BaseModel):
    key: str = ""
    title: str = "Unknown"
    authors: list[_SubjectAuthor] = []
    first_publish_year: int | None = None
    cover_id: int | None = None


class _TrendingPage(BaseModel):
    works: list[_TrendingWork] = []


class _SubjectPage(BaseModel):
    works: list[_SubjectWork] = []


def cover_url(cover_id: int | None, size: str = "M") -> str | None:
    """Full cover image URL for an Open Library cover ID."""
    if not cover_id:
        return None
    return f"{COVER_BASE}/{cover_id}-{size}.jpg"


def _year(first_publish_year: int | None) -> str:
    return str(first_publish_year) if first_publish_year else ""


def _trending_card(work: _TrendingWork) -> BookCard:
    return BookCard(
        display_title=work.title,
        author=", ".join(work.author_name[:2]),
        year=_year(work.first_publish_year),
        poster=cover_url(work.cover_i),
        overview=", ".join(work.subject[:3]),
        ol_key=work.key,
    )


def _subject_card(work: _SubjectWork) -> BookCard:
    return BookCard(
        display_title=work.title,
        author=", ".join(a.name for a in work.authors[:2]),
        year=_year(work.first_publish_year),
        poster=cover_url(work.cover_id),
        overview="",
        ol_key=work.key,
    )


class OpenLibraryClient:
    """Client for the Open Library REST API."""

    BASE_URL = "https://openlibrary.org"

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=15,
                headers={"Accept": "application/json"},
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, endpoint: str) -> bytes:
        resp = await self.client.get(f"{self.BASE_URL}/{endpoint}", params={"limit": 24})
        resp.raise_for_status()
        return resp.content

    async def _trending(self, period: Literal["daily", "weekly"]) -> list[BookCard]:
        page = _TrendingPage.model_validate_json(await self._get(f"trending/{period}.json"))
        return [_trending_card(w) for w in page.works]

    async def get_trending_daily(self) -> list[BookCard]:
        """Books trending today on Open Library."""
        return await self._trending("daily")

    async def get_trending_weekly(self) -> list[BookCard]:
        """Books trending this week on Open Library."""
        return await self._trending("weekly")

    async def get_subject(self, subject: str) -> list[BookCard]:
        """Top books for a genre/subject (e.g. 'fiction', 'mystery')."""
        page = _SubjectPage.model_validate_json(await self._get(f"subjects/{subject}.json"))
        return [_subject_card(w) for w in page.works]
