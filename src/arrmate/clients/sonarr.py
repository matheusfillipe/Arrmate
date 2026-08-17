"""Sonarr API client implementation."""

import logging
from typing import Any

import httpx

from .base_arr import BaseArrClient


class SonarrClient(BaseArrClient):
    """Client for Sonarr v3 API (TV)."""

    entity = "series"
    api_prefix = "api/v3"
    search_command = "SeriesSearch"

    async def add_series(
        self,
        tvdb_id: int,
        title: str,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing_episodes: bool = True,
        season_folder: bool = True,
    ) -> dict[str, Any]:
        """Add a new series to the library."""
        data = {
            "tvdbId": tvdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "seasonFolder": season_folder,
            "addOptions": {"searchForMissingEpisodes": search_for_missing_episodes},
        }
        return await self._post("api/v3/series", data=data)

    async def add_series_from_lookup(
        self,
        lookup_result: dict[str, Any],
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing_episodes: bool = True,
    ) -> dict[str, Any]:
        """Add a series directly from a search/lookup result dict."""
        data = dict(lookup_result)
        data["qualityProfileId"] = quality_profile_id
        data["rootFolderPath"] = root_folder_path
        data["monitored"] = monitored
        data["addOptions"] = {"searchForMissingEpisodes": search_for_missing_episodes}
        return await self._post("api/v3/series", data=data)

    async def get_episodes(
        self, series_id: int, season_number: int | None = None
    ) -> list[dict[str, Any]]:
        """Get episodes for a series, optionally filtered by season."""
        params: dict[str, Any] = {"seriesId": series_id}
        if season_number is not None:
            params["seasonNumber"] = season_number
        return await self._get("api/v3/episode", params=params)

    async def get_episode_files(self, series_id: int) -> list[dict[str, Any]]:
        """Get episode files for a series."""
        return await self._get("api/v3/episodefile", params={"seriesId": series_id})

    async def delete_episode_file(self, file_id: int) -> bool:
        """Delete an episode file."""
        await self._delete(f"api/v3/episodefile/{file_id}")
        return True

    async def delete_episode_files(self, file_ids: list[int]) -> int:
        """Delete episode files, returning the number successfully deleted."""
        deleted = 0
        for file_id in file_ids:
            try:
                await self.delete_episode_file(file_id)
                deleted += 1
            except httpx.HTTPError as e:
                logging.getLogger(__name__).warning(
                    "Failed to delete episode file %s: %s", file_id, e
                )
        return deleted

    async def trigger_season_search(self, series_id: int, season_number: int) -> dict[str, Any]:
        """Trigger a search for one season."""
        return await self._post(
            "api/v3/command",
            data={"name": "SeasonSearch", "seriesId": series_id, "seasonNumber": season_number},
        )

    async def trigger_episode_search(self, episode_ids: list[int]) -> dict[str, Any]:
        """Trigger a search for specific episodes."""
        return await self._post(
            "api/v3/command",
            data={"name": "EpisodeSearch", "episodeIds": episode_ids},
        )

    async def monitor_all_seasons(self, series_id: int) -> dict[str, Any]:
        """Set every season of a series to monitored."""
        series = await self._get(f"api/v3/series/{series_id}")
        series["monitored"] = True
        for season in series.get("seasons", []):
            season["monitored"] = True
        return await self._put(f"api/v3/series/{series_id}", data=series)

    async def set_series_monitored(self, series_id: int, monitored: bool) -> dict[str, Any]:
        """Update the monitored status of a series."""
        series = await self._get(f"api/v3/series/{series_id}")
        series["monitored"] = monitored
        return await self._put(f"api/v3/series/{series_id}", data=series)

    async def get_calendar(
        self, start: str, end: str, include_series: bool = True
    ) -> list[dict[str, Any]]:
        """Get episodes airing between start and end dates."""
        params: dict[str, Any] = {
            "start": start,
            "end": end,
            "includeSeries": str(include_series).lower(),
            "includeEpisodeFile": "false",
        }
        return await self._get("api/v3/calendar", params=params)

    async def get_queue(self, page_size: int = 50) -> dict[str, Any]:
        """Get the current download queue."""
        params: dict[str, Any] = {
            "pageSize": page_size,
            "includeSeries": "true",
            "includeEpisode": "true",
        }
        return await self._get("api/v3/queue", params=params)

    async def get_history(self, page_size: int = 25) -> dict[str, Any]:
        """Get recent download history, newest first."""
        params: dict[str, Any] = {
            "pageSize": page_size,
            "includeSeries": "true",
            "includeEpisode": "true",
            "sortKey": "date",
            "sortDirection": "descending",
        }
        return await self._get("api/v3/history", params=params)

    async def get_wanted_missing(self, page_size: int = 50) -> dict[str, Any]:
        """Get monitored episodes that are missing (no file yet)."""
        params: dict[str, Any] = {
            "pageSize": page_size,
            "includeSeries": "true",
            "sortKey": "airDateUtc",
            "sortDirection": "descending",
        }
        return await self._get("api/v3/wanted/missing", params=params)

    async def trigger_rename_series(self, series_id: int) -> dict[str, Any]:
        """Trigger a rename of all files for a series."""
        return await self._post(
            "api/v3/command",
            data={"name": "RenameSeries", "seriesId": series_id},
        )

    async def rescan_series(self, series_id: int) -> dict[str, Any]:
        """Trigger a disk rescan for a series."""
        return await self._post(
            "api/v3/command",
            data={"name": "RescanSeries", "seriesId": series_id},
        )

    async def interactive_search_episode(self, episode_id: int) -> list[dict[str, Any]]:
        """Run a live interactive indexer search for one episode.

        Queries every indexer in real time; can take 30-180 seconds. Rejected
        releases are included by Sonarr with a ``rejections`` array; callers
        must preserve it, since "Release is blocklisted" on top-seeded results
        is a diagnostic signal, not noise.
        """
        return await self._get_with_timeout("api/v3/release", params={"episodeId": episode_id})

    async def interactive_search_season(
        self, series_id: int, season_number: int
    ) -> list[dict[str, Any]]:
        """Run a live interactive indexer search for a season."""
        return await self._get_with_timeout(
            "api/v3/release",
            params={"seriesId": series_id, "seasonNumber": season_number},
        )

    async def get_episode_history(self, episode_id: int, page_size: int = 50) -> dict[str, Any]:
        """Get history events for one episode, newest first."""
        return await self._get(
            "api/v3/history",
            params={
                "pageSize": page_size,
                "episodeId": episode_id,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
