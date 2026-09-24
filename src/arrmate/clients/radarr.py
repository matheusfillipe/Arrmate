"""Radarr API client implementation."""

from typing import Literal

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

MovieStatus = Literal["tba", "announced", "inCinemas", "released", "deleted"]
RadarrEventType = Literal[
    "unknown",
    "grabbed",
    "downloadFolderImported",
    "downloadFailed",
    "movieFileDeleted",
    "movieFolderImported",
    "movieFileRenamed",
    "downloadIgnored",
]


class Rating(ArrRecord):
    votes: int
    value: float


class MovieRatings(ArrRecord):
    imdb: Rating | None = None
    tmdb: Rating | None = None
    trakt: Rating | None = None
    metacritic: Rating | None = None
    rotten_tomatoes: Rating | None = Field(default=None, alias="rottenTomatoes")


class _MovieFields(ArrRecord):
    title: str
    year: int
    status: MovieStatus
    tmdb_id: int = Field(alias="tmdbId")
    monitored: bool
    imdb_id: str | None = Field(default=None, alias="imdbId")
    overview: str | None = None
    images: list[Image] = Field(default_factory=list)
    remote_poster: str | None = Field(default=None, alias="remotePoster")
    ratings: MovieRatings | None = None
    genres: list[str] = Field(default_factory=list)
    tags: list[int] = Field(default_factory=list)
    in_cinemas: str | None = Field(default=None, alias="inCinemas")
    digital_release: str | None = Field(default=None, alias="digitalRelease")
    physical_release: str | None = Field(default=None, alias="physicalRelease")


class MovieLookup(_MovieFields):
    """A lookup match; ``id`` is set when the movie is already in the library."""

    id: int | None = None


class Movie(_MovieFields):
    id: int
    path: str
    quality_profile_id: int = Field(alias="qualityProfileId")
    #: Radarr leaves these out of the movie it nests in history and queue records.
    has_file: bool | None = Field(default=None, alias="hasFile")
    size_on_disk: int | None = Field(default=None, alias="sizeOnDisk")


class MovieFile(ArrRecord):
    id: int
    movie_id: int = Field(alias="movieId")
    path: str
    size: int


class RadarrHistoryRecord(HistoryRecord):
    event_type: RadarrEventType = Field(alias="eventType")
    movie_id: int = Field(alias="movieId")
    movie: Movie | None = None


class RadarrQueueRecord(QueueRecord):
    movie_id: int | None = Field(default=None, alias="movieId")
    movie: Movie | None = None


class RadarrClient(BaseArrClient[Movie, MovieLookup]):
    """Client for Radarr v3 API (Movies)."""

    entity = "movie"
    api_prefix = "api/v3"
    search_command = "MoviesSearch"

    async def trigger_item_search(self, item_id: int) -> Command:
        """Search for a movie (Radarr takes a list of movie IDs)."""
        return await self._command("MoviesSearch", {"movieIds": [item_id]})

    async def add_movie(
        self,
        tmdb_id: int,
        title: str,
        quality_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_movie: bool = True,
    ) -> Movie:
        raw = await self._post(
            "api/v3/movie",
            data={
                "tmdbId": tmdb_id,
                "title": title,
                "qualityProfileId": quality_profile_id,
                "rootFolderPath": root_folder_path,
                "monitored": monitored,
                "addOptions": {"searchForMovie": search_for_movie},
            },
        )
        return Movie.model_validate(raw)

    async def set_movie_monitored(self, movie_id: int, monitored: bool) -> Movie:
        """Flip the monitored flag.

        We PUT back the raw movie so fields our model does not carry survive the update.
        """
        raw = await self._get(f"api/v3/movie/{movie_id}")
        raw["monitored"] = monitored
        return Movie.model_validate(await self._put(f"api/v3/movie/{movie_id}", data=raw))

    async def get_movie_files(self, movie_id: int) -> list[MovieFile]:
        raw = await self._get("api/v3/moviefile", params={"movieId": movie_id})
        return TypeAdapter(list[MovieFile]).validate_python(raw)

    async def get_calendar(self, start: str, end: str) -> list[Movie]:
        """Movies releasing between start and end dates."""
        raw = await self._get("api/v3/calendar", params={"start": start, "end": end})
        return TypeAdapter(list[Movie]).validate_python(raw)

    async def get_queue(self, page_size: int = 50) -> Page[RadarrQueueRecord]:
        raw = await self._get(
            "api/v3/queue", params={"pageSize": page_size, "includeMovie": "true"}
        )
        return Page[RadarrQueueRecord].model_validate(raw)

    async def get_history(self, page_size: int = 25) -> Page[RadarrHistoryRecord]:
        """Recent download history, newest first."""
        raw = await self._get(
            "api/v3/history",
            params={
                "pageSize": page_size,
                "includeMovie": "true",
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        return Page[RadarrHistoryRecord].model_validate(raw)

    async def get_wanted_cutoff(self, page_size: int = 50) -> Page[Movie]:
        """Monitored movies below their quality cutoff."""
        raw = await self._get(
            "api/v3/wanted/cutoff",
            params={"pageSize": page_size, "sortKey": "title", "sortDirection": "ascending"},
        )
        return Page[Movie].model_validate(raw)

    async def trigger_rename_movie(self, movie_id: int) -> Command:
        """Rename every file of a movie to the naming convention."""
        files = await self.get_movie_files(movie_id)
        return await self._command(
            "RenameFiles", {"movieIds": [movie_id], "files": [file.id for file in files]}
        )

    async def rescan_movie(self, movie_id: int) -> Command:
        return await self._command("RescanMovie", {"movieId": movie_id})

    async def interactive_search(self, movie_id: int) -> list[Release]:
        """Live interactive indexer search for a movie; can take 30-180 seconds."""
        raw = await self._get_with_timeout("api/v3/release", params={"movieId": movie_id})
        return TypeAdapter(list[Release]).validate_python(raw)

    async def get_movie_history(
        self, movie_id: int, page_size: int = 50
    ) -> Page[RadarrHistoryRecord]:
        """History events for one movie, newest first."""
        raw = await self._get(
            "api/v3/history",
            params={
                "pageSize": page_size,
                "movieId": movie_id,
                "sortKey": "date",
                "sortDirection": "descending",
            },
        )
        return Page[RadarrHistoryRecord].model_validate(raw)
