"""Sonarr API client implementation."""

import logging
from typing import Any, Literal

import httpx
from pydantic import Field, TypeAdapter

from .base_arr import (
    ArrRecord,
    BaseArrClient,
    Command,
    HistoryRecord,
    Image,
    Page,
    QueueRecord,
    Release,
)

logger = logging.getLogger(__name__)

SeriesStatus = Literal["continuing", "ended", "upcoming", "deleted"]
SeriesType = Literal["standard", "daily", "anime"]
SonarrEventType = Literal[
    "unknown",
    "grabbed",
    "seriesFolderImported",
    "downloadFolderImported",
    "downloadFailed",
    "episodeFileDeleted",
    "episodeFileRenamed",
    "downloadIgnored",
]


class Season(ArrRecord):
    season_number: int = Field(alias="seasonNumber")
    monitored: bool


class SeriesStatistics(ArrRecord):
    season_count: int = Field(alias="seasonCount")
    episode_file_count: int = Field(alias="episodeFileCount")
    episode_count: int = Field(alias="episodeCount")
    total_episode_count: int = Field(alias="totalEpisodeCount")
    size_on_disk: int = Field(alias="sizeOnDisk")


class SeriesRatings(ArrRecord):
    votes: int
    value: float


class _SeriesFields(ArrRecord):
    title: str
    year: int
    status: SeriesStatus
    series_type: SeriesType = Field(alias="seriesType")
    tvdb_id: int = Field(alias="tvdbId")
    #: Sonarr sends 0 when TMDB has no match.
    tmdb_id: int = Field(default=0, alias="tmdbId")
    monitored: bool
    overview: str | None = None
    network: str | None = None
    images: list[Image] = Field(default_factory=list)
    remote_poster: str | None = Field(default=None, alias="remotePoster")
    ratings: SeriesRatings | None = None
    statistics: SeriesStatistics | None = None
    seasons: list[Season] = Field(default_factory=list)
    genres: list[str] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)


class SeriesLookup(_SeriesFields):
    """A lookup match; ``id`` is set when the series is already in the library."""

    id: int | None = None


class Series(_SeriesFields):
    id: int
    path: str
    quality_profile_id: int = Field(alias="qualityProfileId")


class Episode(ArrRecord):
    id: int
    series_id: int = Field(alias="seriesId")
    season_number: int = Field(alias="seasonNumber")
    episode_number: int = Field(alias="episodeNumber")
    title: str
    has_file: bool = Field(alias="hasFile")
    monitored: bool
    #: Sonarr sends 0 for an episode without a file.
    episode_file_id: int = Field(default=0, alias="episodeFileId")
    air_date: str | None = Field(default=None, alias="airDate")
    air_date_utc: str | None = Field(default=None, alias="airDateUtc")
    series: Series | None = None

    @property
    def label(self) -> str:
        return f"S{self.season_number:02d}E{self.episode_number:02d}"


class SonarrHistoryRecord(HistoryRecord):
    event_type: SonarrEventType = Field(alias="eventType")
    series_id: int = Field(alias="seriesId")
    episode_id: int = Field(alias="episodeId")
    series: Series | None = None
    episode: Episode | None = None


class SonarrQueueRecord(QueueRecord):
    series_id: int | None = Field(default=None, alias="seriesId")
    episode_id: int | None = Field(default=None, alias="episodeId")
    series: Series | None = None
    episode: Episode | None = None


class SonarrClient(BaseArrClient[Series, SeriesLookup]):
    """Client for Sonarr v3 API (TV)."""

    entity = "series"
    api_prefix = "api/v3"
    search_command = "SeriesSearch"

    async def add_series(
        self,
        tvdb_id: int,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing_episodes: bool = True,
    ) -> Series:
        """Add a series by its TVDB id.

        Sonarr needs the whole lookup object (titleSlug, seasons, images) in the add
        request, so we post back the raw lookup result with our settings on top.
        """
        matches = await self._get("api/v3/series/lookup", params={"term": f"tvdb:{tvdb_id}"})
        if not matches:
            raise ValueError(f"Sonarr has no series with TVDB id {tvdb_id}")
        payload = matches[0]
        payload["qualityProfileId"] = quality_profile_id
        payload["rootFolderPath"] = root_folder_path
        payload["monitored"] = monitored
        payload["addOptions"] = {"searchForMissingEpisodes": search_for_missing_episodes}
        return Series.model_validate(await self._post("api/v3/series", data=payload))

    async def get_episodes(self, series_id: int, season_number: int | None = None) -> list[Episode]:
        """Episodes of a series, optionally only one season."""
        params = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        raw = await self._get("api/v3/episode", params=params)
        return TypeAdapter(list[Episode]).validate_python(raw)

    async def delete_episode_file(self, file_id: int) -> None:
        await self._delete(f"api/v3/episodefile/{file_id}")

    async def delete_episode_files(self, file_ids: list[int]) -> int:
        """Delete episode files, returning the number successfully deleted."""
        deleted = 0
        for file_id in file_ids:
            try:
                await self.delete_episode_file(file_id)
                deleted += 1
            except httpx.HTTPError as e:
                logger.warning("Failed to delete episode file %s: %s", file_id, e)
        return deleted

    async def trigger_season_search(self, series_id: int, season_number: int) -> Command:
        return await self._command(
            "SeasonSearch", {"seriesId": series_id, "seasonNumber": season_number}
        )

    async def trigger_episode_search(self, episode_ids: list[int]) -> Command:
        return await self._command("EpisodeSearch", {"episodeIds": episode_ids})

    async def _get_raw_series(self, series_id: int) -> dict[str, Any]:
        """The series exactly as Sonarr sends it.

        Updates PUT the whole object back, so we edit this raw copy to keep the fields
        our model does not carry.
        """
        raw: dict[str, Any] = await self._get(f"api/v3/series/{series_id}")
        return raw

    async def _put_series(self, raw: dict[str, Any]) -> Series:
        return Series.model_validate(await self._put(f"api/v3/series/{raw['id']}", data=raw))

    async def set_series_monitored(self, series_id: int, monitored: bool) -> Series:
        raw = await self._get_raw_series(series_id)
        raw["monitored"] = monitored
        return await self._put_series(raw)

    async def set_season_monitored(self, series_id: int, season: int, monitored: bool) -> Series:
        raw = await self._get_raw_series(series_id)
        for raw_season in raw.get("seasons", []):
            if raw_season.get("seasonNumber") == season:
                raw_season["monitored"] = monitored
        return await self._put_series(raw)

    async def monitor_all_seasons(self, series_id: int) -> Series:
        """Set the series and every season of it to monitored."""
        raw = await self._get_raw_series(series_id)
        raw["monitored"] = True
        for raw_season in raw.get("seasons", []):
            raw_season["monitored"] = True
        return await self._put_series(raw)

    async def get_calendar(self, start: str, end: str) -> list[Episode]:
        """Episodes airing between start and end dates, each with its series."""
        raw = await self._get(
            "api/v3/calendar",
            params={
                "start": start,
                "end": end,
                "includeSeries": "true",
                "includeEpisodeFile": "false",
            },
        )
        return TypeAdapter(list[Episode]).validate_python(raw)

    async def get_queue(self, page_size: int = 50) -> Page[SonarrQueueRecord]:
        raw = await self._get(
            "api/v3/queue",
            params={"pageSize": page_size, "includeSeries": "true", "includeEpisode": "true"},
        )
        return Page[SonarrQueueRecord].model_validate(raw)

    async def get_history(self, page_size: int = 25) -> Page[SonarrHistoryRecord]:
        """Recent download history, newest first."""
        raw = await self._get(
            "api/v3/history",
            params={
                "pageSize": page_size,
                "includeSeries": "true",
                "includeEpisode": "true",
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        return Page[SonarrHistoryRecord].model_validate(raw)

    async def get_wanted_missing(self, page_size: int = 50) -> Page[Episode]:
        """Monitored episodes that have aired and have no file yet."""
        raw = await self._get(
            "api/v3/wanted/missing",
            params={
                "pageSize": page_size,
                "includeSeries": "true",
                "sortKey": "airDateUtc",
                "sortDirection": "descending",
            },
        )
        return Page[Episode].model_validate(raw)

    async def trigger_rename_series(self, series_id: int) -> Command:
        return await self._command("RenameSeries", {"seriesId": series_id})

    async def rescan_series(self, series_id: int) -> Command:
        return await self._command("RescanSeries", {"seriesId": series_id})

    async def interactive_search_episode(self, episode_id: int) -> list[Release]:
        """Live interactive indexer search for one episode; can take 30-180 seconds."""
        raw = await self._get_with_timeout("api/v3/release", params={"episodeId": episode_id})
        return TypeAdapter(list[Release]).validate_python(raw)

    async def interactive_search_season(self, series_id: int, season_number: int) -> list[Release]:
        """Live interactive indexer search for a season."""
        raw = await self._get_with_timeout(
            "api/v3/release",
            params={"seriesId": series_id, "seasonNumber": season_number},
        )
        return TypeAdapter(list[Release]).validate_python(raw)

    async def get_episode_history(
        self, episode_id: int, page_size: int = 50
    ) -> Page[SonarrHistoryRecord]:
        """History events for one episode, newest first."""
        raw = await self._get(
            "api/v3/history",
            params={
                "pageSize": page_size,
                "episodeId": episode_id,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        return Page[SonarrHistoryRecord].model_validate(raw)
