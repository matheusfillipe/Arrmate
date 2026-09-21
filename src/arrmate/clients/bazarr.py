"""Bazarr subtitle manager client.

Bazarr follows the Sonarr and Radarr libraries and addresses items by their ids there:
``sonarrSeriesId``/``sonarrEpisodeId`` for TV and ``radarrId`` for movies. Its write
endpoints take form fields.
"""

from typing import Any, Literal

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter

from .base_companion import BaseCompanionClient

JobStatus = Literal["pending", "running", "failed", "completed"]


class _BazarrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class Subtitle(_BazarrRecord):
    code2: str
    hi: bool = False
    forced: bool = False
    #: Bazarr stores a path only for subtitle files it finds beside the video.
    path: str | None = None

    @property
    def is_embedded(self) -> bool:
        return self.path is None


class Episode(_BazarrRecord):
    season: int
    episode: int
    sonarr_episode_id: int = Field(alias="sonarrEpisodeId")
    subtitles: list[Subtitle] = []
    missing_subtitles: list[Subtitle] = []


class Series(_BazarrRecord):
    sonarr_series_id: int = Field(alias="sonarrSeriesId")
    title: str
    profile_id: int | None = Field(default=None, alias="profileId")
    episode_file_count: int = Field(default=0, alias="episodeFileCount")
    episode_missing_count: int = Field(default=0, alias="episodeMissingCount")


class Movie(_BazarrRecord):
    radarr_id: int = Field(alias="radarrId")
    title: str
    profile_id: int | None = Field(default=None, alias="profileId")
    subtitles: list[Subtitle] = []
    missing_subtitles: list[Subtitle] = []


class WantedEpisode(_BazarrRecord):
    series_title: str = Field(alias="seriesTitle")
    sonarr_series_id: int = Field(alias="sonarrSeriesId")
    episode_number: str
    missing_subtitles: list[Subtitle] = []


class HistoryEntry(_BazarrRecord):
    title: str | None = Field(default=None, validation_alias=AliasChoices("seriesTitle", "title"))
    episode_number: str | None = None
    parsed_timestamp: str | None = None
    description: str | None = None
    provider: str | None = None
    score: str | None = None
    subtitles_path: str | None = None


class ProfileLanguage(_BazarrRecord):
    language: str


class LanguageProfile(_BazarrRecord):
    profile_id: int = Field(alias="profileId")
    name: str
    items: list[ProfileLanguage] = []


class Provider(_BazarrRecord):
    name: str
    status: str
    retry: str


class Job(_BazarrRecord):
    job_name: str
    status: JobStatus
    progress_value: int = 0
    progress_max: int = 0
    progress_message: str | None = None


_EPISODES = TypeAdapter(list[Episode])
_SERIES = TypeAdapter(list[Series])
_MOVIES = TypeAdapter(list[Movie])
_WANTED_EPISODES = TypeAdapter(list[WantedEpisode])
_HISTORY = TypeAdapter(list[HistoryEntry])
_PROFILES = TypeAdapter(list[LanguageProfile])
_PROVIDERS = TypeAdapter(list[Provider])
_JOBS = TypeAdapter(list[Job])


def _profile_field(profile_id: int | None) -> int | str:
    """Bazarr reads an empty profile id as "no profile", which stops it wanting subtitles."""
    return "" if profile_id is None else profile_id


class BazarrClient(BaseCompanionClient):
    """Client for the Bazarr API."""

    async def test_connection(self) -> bool:
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def _send_form(self, method: str, endpoint: str, form: dict[str, int | str]) -> None:
        response = await self.client.request(method, f"{self.base_url}/{endpoint}", data=form)
        response.raise_for_status()

    async def _get_data(self, endpoint: str, params: dict[str, int | str] | None = None) -> Any:
        return (await self._get(endpoint, params=params))["data"]

    async def get_missing_items(self, service_type: str) -> list[dict[str, Any]]:
        if service_type.lower() == "sonarr":
            return await self._get_data("api/episodes/wanted")
        if service_type.lower() == "radarr":
            return await self._get_data("api/movies/wanted")
        raise ValueError(f"Unsupported service type: {service_type}")

    async def get_series(self) -> list[Series]:
        return _SERIES.validate_python(await self._get_data("api/series"))

    async def get_movies(self) -> list[Movie]:
        return _MOVIES.validate_python(await self._get_data("api/movies"))

    async def get_episodes(self, series_id: int) -> list[Episode]:
        data = await self._get_data("api/episodes", params={"seriesid[]": series_id})
        return _EPISODES.validate_python(data)

    async def get_wanted_episodes(self) -> list[WantedEpisode]:
        return _WANTED_EPISODES.validate_python(await self._get_data("api/episodes/wanted"))

    async def get_wanted_movies(self) -> list[Movie]:
        return _MOVIES.validate_python(await self._get_data("api/movies/wanted"))

    async def get_episode_history(self, length: int) -> list[HistoryEntry]:
        data = await self._get_data("api/episodes/history", params={"length": length})
        return _HISTORY.validate_python(data)

    async def get_movie_history(self, length: int) -> list[HistoryEntry]:
        data = await self._get_data("api/movies/history", params={"length": length})
        return _HISTORY.validate_python(data)

    async def get_providers(self) -> list[Provider]:
        return _PROVIDERS.validate_python(await self._get_data("api/providers"))

    async def get_language_profiles(self) -> list[LanguageProfile]:
        return _PROFILES.validate_python(await self._get("api/system/languages/profiles"))

    async def get_jobs(self, status: JobStatus) -> list[Job]:
        return _JOBS.validate_python(
            await self._get_data("api/system/jobs", params={"status": status})
        )

    async def search_series(self, series_id: int) -> None:
        """Queue a provider search for every subtitle a series is missing."""
        await self._send_form(
            "PATCH", "api/series", {"seriesid": series_id, "action": "search-missing"}
        )

    async def search_movie(self, radarr_id: int) -> None:
        await self._send_form(
            "PATCH", "api/movies", {"radarrid": radarr_id, "action": "search-missing"}
        )

    async def set_series_profile(self, series_id: int, profile_id: int | None) -> None:
        await self._send_form(
            "POST", "api/series", {"seriesid": series_id, "profileid": _profile_field(profile_id)}
        )

    async def set_movie_profile(self, radarr_id: int, profile_id: int | None) -> None:
        await self._send_form(
            "POST", "api/movies", {"radarrid": radarr_id, "profileid": _profile_field(profile_id)}
        )
