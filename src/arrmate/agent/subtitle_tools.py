"""Subtitle tools over Bazarr.

Bazarr keys TV by Sonarr's series and episode ids and movies by Radarr's id, and wants
subtitles only for items that carry a language profile. A subtitle track embedded in the
video file already counts as present.
"""

from typing import Literal, assert_never

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from arrmate.agent.deps import AgentDeps
from arrmate.agent.tools import _safe
from arrmate.clients.bazarr import (
    Episode,
    HistoryEntry,
    Job,
    JobStatus,
    LanguageProfile,
    Movie,
    Provider,
    Subtitle,
)
from arrmate.core.models import MediaType

SubtitleMediaType = Literal[MediaType.TV, MediaType.MOVIE]

_ACTIVE_JOB_STATES: tuple[JobStatus, ...] = ("pending", "running")
_NO_PROFILE = "none"


class EpisodeSubtitles(BaseModel):
    episode: str
    episode_id: int
    has: list[str]
    missing: list[str]


class MovieSubtitles(BaseModel):
    id: int
    title: str
    profile: str
    has: list[str]
    missing: list[str]


class SeriesSubtitles(BaseModel):
    id: int
    title: str
    profile: str
    episodes_with_files: int
    episodes_missing_subtitles: int


class WantedEpisodeSubtitles(BaseModel):
    series: str
    series_id: int
    episode: str
    missing: list[str]


class SearchQueued(BaseModel):
    media_type: SubtitleMediaType
    id: int
    queued: bool = True


class ProfileAssigned(BaseModel):
    media_type: SubtitleMediaType
    id: int
    profile: str


class UnknownProfile(BaseModel):
    error: str = "unknown-profile"
    known_profiles: dict[int, str]


class JobProgress(BaseModel):
    name: str
    status: JobStatus
    progress: str
    message: str | None


class ProfileSummary(BaseModel):
    id: int
    name: str
    languages: list[str]


class SubtitleSetup(BaseModel):
    profiles: list[ProfileSummary]
    providers: list[Provider]


def subtitle_label(subtitle: Subtitle) -> str:
    label = subtitle.code2
    if subtitle.hi:
        label += ":hi"
    if subtitle.forced:
        label += ":forced"
    return label


def _present_labels(subtitles: list[Subtitle]) -> list[str]:
    return [subtitle_label(sub) + (":embedded" if sub.is_embedded else "") for sub in subtitles]


def _missing_labels(subtitles: list[Subtitle]) -> list[str]:
    return [subtitle_label(sub) for sub in subtitles]


def summarize_episode(episode: Episode) -> EpisodeSubtitles:
    return EpisodeSubtitles(
        episode=f"S{episode.season:02d}E{episode.episode:02d}",
        episode_id=episode.sonarr_episode_id,
        has=_present_labels(episode.subtitles),
        missing=_missing_labels(episode.missing_subtitles),
    )


def summarize_movie(movie: Movie, profile_names: dict[int, str]) -> MovieSubtitles:
    return MovieSubtitles(
        id=movie.radarr_id,
        title=movie.title,
        profile=profile_names.get(movie.profile_id or 0, _NO_PROFILE),
        has=_present_labels(movie.subtitles),
        missing=_missing_labels(movie.missing_subtitles),
    )


def _profile_names(profiles: list[LanguageProfile]) -> dict[int, str]:
    return {profile.profile_id: profile.name for profile in profiles}


def _matches(title: str, needle: str) -> bool:
    return needle.casefold() in title.casefold()


def _job_progress(job: Job) -> JobProgress:
    return JobProgress(
        name=job.job_name,
        status=job.status,
        progress=f"{job.progress_value}/{job.progress_max}",
        message=job.progress_message,
    )


def register_subtitle_tools(agent: Agent[AgentDeps, str]) -> None:
    """Register the Bazarr subtitle tools on the given Agent."""

    @agent.tool
    async def subtitles_library(
        ctx: RunContext[AgentDeps], media_type: SubtitleMediaType, title_filter: str = ""
    ) -> str:
        """List series or movies as Bazarr sees them: their language profile and what
        subtitles they lack. The returned id is what the other subtitles_* tools
        take (Sonarr series id for TV, Radarr id for movies)."""

        async def body() -> list[SeriesSubtitles] | list[MovieSubtitles]:
            async with ctx.deps.bazarr() as client:
                names = _profile_names(await client.get_language_profiles())
                match media_type:
                    case MediaType.TV:
                        return [
                            SeriesSubtitles(
                                id=series.sonarr_series_id,
                                title=series.title,
                                profile=names.get(series.profile_id or 0, _NO_PROFILE),
                                episodes_with_files=series.episode_file_count,
                                episodes_missing_subtitles=series.episode_missing_count,
                            )
                            for series in await client.get_series()
                            if _matches(series.title, title_filter)
                        ]
                    case MediaType.MOVIE:
                        return [
                            summarize_movie(movie, names)
                            for movie in await client.get_movies()
                            if _matches(movie.title, title_filter)
                        ]
                    case _:
                        assert_never(media_type)

        return await _safe(body)

    @agent.tool
    async def subtitles_episodes(ctx: RunContext[AgentDeps], series_id: int) -> str:
        """Per-episode subtitles of one series: which languages each episode has,
        embedded or external, and which it is still missing."""

        async def body() -> list[EpisodeSubtitles]:
            async with ctx.deps.bazarr() as client:
                return [summarize_episode(ep) for ep in await client.get_episodes(series_id)]

        return await _safe(body)

    @agent.tool
    async def subtitles_wanted(ctx: RunContext[AgentDeps], media_type: SubtitleMediaType) -> str:
        """Everything Bazarr still wants a subtitle for, across the whole library."""

        async def body() -> list[WantedEpisodeSubtitles] | list[MovieSubtitles]:
            async with ctx.deps.bazarr() as client:
                match media_type:
                    case MediaType.TV:
                        return [
                            WantedEpisodeSubtitles(
                                series=wanted.series_title,
                                series_id=wanted.sonarr_series_id,
                                episode=wanted.episode_number,
                                missing=_missing_labels(wanted.missing_subtitles),
                            )
                            for wanted in await client.get_wanted_episodes()
                        ]
                    case MediaType.MOVIE:
                        names = _profile_names(await client.get_language_profiles())
                        return [
                            summarize_movie(movie, names)
                            for movie in await client.get_wanted_movies()
                        ]
                    case _:
                        assert_never(media_type)

        return await _safe(body)

    @agent.tool
    async def subtitles_search(
        ctx: RunContext[AgentDeps], media_type: SubtitleMediaType, item_id: int
    ) -> str:
        """Queue a search of the subtitle providers for everything one series or movie
        is missing. Bazarr downloads the best matches in the background: follow it
        with subtitles_jobs, then read the result with subtitles_episodes or
        subtitles_library once the job is gone."""

        async def body() -> SearchQueued:
            ctx.deps.require_write("subtitles_search")
            async with ctx.deps.bazarr() as client:
                match media_type:
                    case MediaType.TV:
                        await client.search_series(item_id)
                    case MediaType.MOVIE:
                        await client.search_movie(item_id)
                    case _:
                        assert_never(media_type)
            return SearchQueued(media_type=media_type, id=item_id)

        return await _safe(body)

    @agent.tool
    async def subtitles_jobs(ctx: RunContext[AgentDeps]) -> str:
        """Bazarr's pending and running background jobs (searches, syncs, disk scans)
        with progress. An empty list means everything queued has finished."""

        async def body() -> list[JobProgress]:
            async with ctx.deps.bazarr() as client:
                return [
                    _job_progress(job)
                    for status in _ACTIVE_JOB_STATES
                    for job in await client.get_jobs(status)
                ]

        return await _safe(body)

    @agent.tool
    async def subtitles_set_profile(
        ctx: RunContext[AgentDeps],
        media_type: SubtitleMediaType,
        item_id: int,
        profile_id: int | None,
    ) -> str:
        """Choose which subtitle languages Bazarr wants for one series or movie, by
        language profile id from subtitles_setup. profile_id null removes the
        profile, so Bazarr stops wanting subtitles for it."""

        async def body() -> ProfileAssigned | UnknownProfile:
            ctx.deps.require_write("subtitles_set_profile")
            async with ctx.deps.bazarr() as client:
                names = _profile_names(await client.get_language_profiles())
                if profile_id is not None and profile_id not in names:
                    return UnknownProfile(known_profiles=names)
                match media_type:
                    case MediaType.TV:
                        await client.set_series_profile(item_id, profile_id)
                    case MediaType.MOVIE:
                        await client.set_movie_profile(item_id, profile_id)
                    case _:
                        assert_never(media_type)
            profile = _NO_PROFILE if profile_id is None else names[profile_id]
            return ProfileAssigned(media_type=media_type, id=item_id, profile=profile)

        return await _safe(body)

    @agent.tool
    async def subtitles_setup(ctx: RunContext[AgentDeps]) -> str:
        """Bazarr's language profiles (which languages each one wants) and the
        status of every enabled subtitle provider."""

        async def body() -> SubtitleSetup:
            async with ctx.deps.bazarr() as client:
                profiles = await client.get_language_profiles()
                providers = await client.get_providers()
            return SubtitleSetup(
                profiles=[
                    ProfileSummary(
                        id=profile.profile_id,
                        name=profile.name,
                        languages=[item.language for item in profile.items],
                    )
                    for profile in profiles
                ],
                providers=providers,
            )

        return await _safe(body)

    @agent.tool
    async def subtitles_history(
        ctx: RunContext[AgentDeps], media_type: SubtitleMediaType, limit: int = 30
    ) -> str:
        """Recent subtitle downloads: which provider, match score, and file written."""

        async def body() -> list[HistoryEntry]:
            async with ctx.deps.bazarr() as client:
                match media_type:
                    case MediaType.TV:
                        return await client.get_episode_history(limit)
                    case MediaType.MOVIE:
                        return await client.get_movie_history(limit)
                    case _:
                        assert_never(media_type)

        return await _safe(body)
