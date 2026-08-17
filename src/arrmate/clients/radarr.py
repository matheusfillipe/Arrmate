"""Radarr API client implementation."""

from typing import Any

from .base_arr import BaseArrClient


class RadarrClient(BaseArrClient):
    """Client for Radarr v3 API (Movies)."""

    entity = "movie"
    api_prefix = "api/v3"
    search_command = "MoviesSearch"

    async def trigger_item_search(self, movie_id: int) -> dict[str, Any]:
        """Trigger a search for a movie (Radarr takes a list of movie IDs)."""
        return await self._post(
            "api/v3/command",
            data={"name": "MoviesSearch", "movieIds": [movie_id]},
        )

    async def add_movie(
        self,
        tmdb_id: int,
        title: str,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_movie: bool = True,
    ) -> dict[str, Any]:
        """Add a new movie to the library."""
        data = {
            "tmdbId": tmdb_id,
            "title": title,
            "qualityProfileId": quality_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"searchForMovie": search_for_movie},
        }
        return await self._post("api/v3/movie", data=data)

    async def get_movie_file(self, movie_id: int) -> dict[str, Any]:
        """Get the file details of a movie."""
        files = await self._get("api/v3/moviefile", params={"movieId": movie_id})
        return files[0] if files else {}

    async def delete_movie_file(self, file_id: int) -> bool:
        """Delete a movie file."""
        await self._delete(f"api/v3/moviefile/{file_id}")
        return True

    async def set_movie_monitored(self, movie_id: int, monitored: bool) -> dict[str, Any]:
        """Update the monitored status of a movie."""
        movie = await self._get(f"api/v3/movie/{movie_id}")
        movie["monitored"] = monitored
        return await self._put(f"api/v3/movie/{movie_id}", data=movie)

    async def get_all_movies_with_files(self) -> list[dict[str, Any]]:
        """Get all movies including nested movieFile with mediaInfo."""
        return await self._get("api/v3/movie")

    async def get_calendar(self, start: str, end: str) -> list[dict[str, Any]]:
        """Get movies releasing between start and end dates."""
        params: dict[str, Any] = {"start": start, "end": end}
        return await self._get("api/v3/calendar", params=params)

    async def get_queue(self, page_size: int = 50) -> dict[str, Any]:
        """Get the current download queue."""
        params: dict[str, Any] = {"pageSize": page_size, "includeMovie": "true"}
        return await self._get("api/v3/queue", params=params)

    async def get_history(self, page_size: int = 25) -> dict[str, Any]:
        """Get recent download history, newest first."""
        params: dict[str, Any] = {
            "pageSize": page_size,
            "includeMovie": "true",
            "sortKey": "date",
            "sortDirection": "descending",
        }
        return await self._get("api/v3/history", params=params)

    async def get_wanted_cutoff(self, page_size: int = 50) -> dict[str, Any]:
        """Get monitored movies below quality cutoff."""
        params: dict[str, Any] = {
            "pageSize": page_size,
            "sortKey": "title",
            "sortDirection": "ascending",
        }
        return await self._get("api/v3/wanted/cutoff", params=params)

    async def trigger_rename_movie(self, movie_id: int) -> dict[str, Any]:
        """Trigger a rename of all files for a movie."""
        files = await self._get("api/v3/moviefile", params={"movieId": movie_id})
        file_ids = [file["id"] for file in files if file.get("id")]
        return await self._post(
            "api/v3/command",
            data={"name": "RenameFiles", "movieIds": [movie_id], "files": file_ids},
        )

    async def rescan_movie(self, movie_id: int) -> dict[str, Any]:
        """Trigger a disk rescan for a movie."""
        return await self._post(
            "api/v3/command",
            data={"name": "RescanMovie", "movieId": movie_id},
        )

    async def interactive_search(self, movie_id: int) -> list[dict[str, Any]]:
        """Run a live interactive indexer search for a movie.

        Queries every indexer in real time; can take 30-180 seconds. Rejected
        releases are included by Radarr with a ``rejections`` array; callers
        must preserve it, since "Release is blocklisted" on top-seeded results
        is a diagnostic signal, not noise.
        """
        return await self._get_with_timeout("api/v3/release", params={"movieId": movie_id})

    async def get_movie_history(self, movie_id: int, page_size: int = 50) -> dict[str, Any]:
        """Get history events for one movie, newest first."""
        return await self._get(
            "api/v3/history",
            params={
                "pageSize": page_size,
                "movieId": movie_id,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
