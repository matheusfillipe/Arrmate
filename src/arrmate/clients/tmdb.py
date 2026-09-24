"""TMDB (The Movie Database) API client.

Used to power the Discover page — trending, upcoming, popular, and on-the-air
content that users can add directly to Radarr/Sonarr.
"""

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

IMAGE_BASE = "https://image.tmdb.org/t/p"

TimeWindow = Literal["day", "week"]


class _TMDBRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _TMDBTitle(_TMDBRecord):
    id: int
    overview: str | None = None
    poster_path: str | None = None
    vote_average: float | None = None
    #: Set by the Discover page after it checks the Radarr or Sonarr library.
    in_library: bool = False

    @property
    def poster(self) -> str | None:
        return f"{IMAGE_BASE}/w342{self.poster_path}" if self.poster_path else None

    @property
    def rating(self) -> float:
        return round(self.vote_average or 0, 1)


class TMDBMovie(_TMDBTitle):
    media_type: Literal["movie"] = "movie"
    title: str
    release_date: str | None = None

    @property
    def display_title(self) -> str:
        return self.title

    @property
    def year(self) -> str:
        return (self.release_date or "")[:4]


class TMDBShow(_TMDBTitle):
    media_type: Literal["tv"] = "tv"
    name: str
    first_air_date: str | None = None

    @property
    def display_title(self) -> str:
        return self.name

    @property
    def year(self) -> str:
        return (self.first_air_date or "")[:4]


class _MoviePage(_TMDBRecord):
    results: list[TMDBMovie]


class _ShowPage(_TMDBRecord):
    results: list[TMDBShow]


class TMDBExternalIds(_TMDBRecord):
    imdb_id: str | None = None
    tvdb_id: int | None = None


class TMDBClient:
    """Client for the TMDB v3 API."""

    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=15)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, endpoint: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        response = await self.client.get(url, params={**(params or {}), "api_key": self.api_key})
        response.raise_for_status()
        return response.json()

    async def _movies(self, endpoint: str, region: str | None = None) -> list[TMDBMovie]:
        params = {"language": "en-US"}
        if region:
            params["region"] = region
        return _MoviePage.model_validate(await self._get(endpoint, params)).results

    async def _shows(self, endpoint: str) -> list[TMDBShow]:
        data = await self._get(endpoint, {"language": "en-US"})
        return _ShowPage.model_validate(data).results

    async def test_connection(self) -> bool:
        try:
            await self._get("configuration")
            return True
        except (httpx.HTTPError, ValueError):
            return False

    # ── Movies ────────────────────────────────────────────────────────────────

    async def get_trending_movies(self, time_window: TimeWindow = "week") -> list[TMDBMovie]:
        """Trending movies over the last day or week."""
        return await self._movies(f"trending/movie/{time_window}")

    async def get_upcoming_movies(self) -> list[TMDBMovie]:
        """Movies with a future release date (US region)."""
        return await self._movies("movie/upcoming", region="US")

    async def get_now_playing(self) -> list[TMDBMovie]:
        """Movies currently in theatres (US region)."""
        return await self._movies("movie/now_playing", region="US")

    async def get_popular_movies(self) -> list[TMDBMovie]:
        """Most popular movies right now."""
        return await self._movies("movie/popular")

    async def get_top_rated_movies(self) -> list[TMDBMovie]:
        """Top-rated movies of all time."""
        return await self._movies("movie/top_rated")

    # ── TV Shows ──────────────────────────────────────────────────────────────

    async def get_trending_tv(self, time_window: TimeWindow = "week") -> list[TMDBShow]:
        """Trending TV shows over the last day or week."""
        return await self._shows(f"trending/tv/{time_window}")

    async def get_tv_airing_today(self) -> list[TMDBShow]:
        """TV shows with episodes airing today."""
        return await self._shows("tv/airing_today")

    async def get_tv_on_the_air(self) -> list[TMDBShow]:
        """TV shows currently airing (next 7 days)."""
        return await self._shows("tv/on_the_air")

    async def get_popular_tv(self) -> list[TMDBShow]:
        """Most popular TV shows right now."""
        return await self._shows("tv/popular")

    async def get_top_rated_tv(self) -> list[TMDBShow]:
        """Top-rated TV shows of all time."""
        return await self._shows("tv/top_rated")

    # ── Metadata helpers ──────────────────────────────────────────────────────

    async def get_external_ids(
        self, tmdb_id: int, media_type: Literal["movie", "tv"] = "tv"
    ) -> TMDBExternalIds:
        """Get external IDs for a movie or TV show.

        For TV shows this includes the TVDB ID needed by Sonarr.
        """
        data = await self._get(f"{media_type}/{tmdb_id}/external_ids")
        return TMDBExternalIds.model_validate(data)
