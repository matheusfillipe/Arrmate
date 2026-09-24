"""Last.fm API client for music discovery.

Requires a free Last.fm API key: https://www.last.fm/api/account/create
Set LASTFM_API_KEY in your environment.
"""

from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

_IMAGE_SIZES_LARGEST_FIRST = ("extralarge", "large", "medium", "small")


class _LastFMRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class LastFMImage(_LastFMRecord):
    url: str = Field(alias="#text")
    size: str


class LastFMArtistRef(_LastFMRecord):
    name: str


class LastFMArtist(_LastFMRecord):
    name: str
    listeners: str | None = None
    mbid: str | None = None
    url: str | None = None
    image: list[LastFMImage] = []


class LastFMTrack(_LastFMRecord):
    name: str
    listeners: str | None = None
    url: str | None = None
    artist: LastFMArtistRef
    image: list[LastFMImage] = []


class _ArtistList(_LastFMRecord):
    artist: list[LastFMArtist]


class _ArtistChart(_LastFMRecord):
    artists: _ArtistList


class _TrackList(_LastFMRecord):
    track: list[LastFMTrack]


class _TrackChart(_LastFMRecord):
    tracks: _TrackList


class MusicCard(BaseModel):
    """One Discover page card for an artist or a track."""

    display_title: str
    artist: str
    listeners: str
    poster: str | None = None
    url: str | None = None
    mbid: str | None = None
    overview: str
    media_type: Literal["music"] = "music"
    in_library: bool = False


class LastFMClient:
    """Client for the Last.fm v2 API (read-only chart/discovery endpoints)."""

    BASE_URL = "http://ws.audioscrobbler.com/2.0/"

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

    async def _get(self, method: str) -> Any:
        params: dict[str, str | int] = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
            "limit": 24,
        }
        resp = await self.client.get(self.BASE_URL, params=params)
        resp.raise_for_status()
        return resp.json()

    async def get_top_artists(self) -> list[MusicCard]:
        """Global top artists chart."""
        chart = _ArtistChart.model_validate(await self._get("chart.gettopartists"))
        return [
            MusicCard(
                display_title=a.name,
                artist=a.name,
                listeners=_fmt_listeners(a.listeners or ""),
                poster=_largest_image(a.image),
                url=a.url,
                mbid=a.mbid,
                overview=f"{_fmt_listeners(a.listeners or '')} listeners",
            )
            for a in chart.artists.artist
        ]

    async def get_top_tracks(self) -> list[MusicCard]:
        """Global top tracks chart."""
        chart = _TrackChart.model_validate(await self._get("chart.gettoptracks"))
        return [
            MusicCard(
                display_title=t.name,
                artist=t.artist.name,
                listeners=_fmt_listeners(t.listeners or ""),
                poster=_largest_image(t.image),
                url=t.url,
                overview=f"by {t.artist.name} · {_fmt_listeners(t.listeners or '')} listeners",
            )
            for t in chart.tracks.track
        ]


def _largest_image(images: list[LastFMImage]) -> str | None:
    """Return the largest non-empty image URL from a Last.fm image list."""
    for size in _IMAGE_SIZES_LARGEST_FIRST:
        for image in images:
            if image.size == size and image.url:
                return image.url
    return None


def _fmt_listeners(raw: str) -> str:
    """Format a raw listener count string, e.g. '5234567' → '5.2M'."""
    try:
        n = int(raw)
    except ValueError:
        return raw
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)
