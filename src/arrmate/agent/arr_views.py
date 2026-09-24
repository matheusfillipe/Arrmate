"""Slim views of Sonarr and Radarr records for agent tool results.

A whole library has to fit in one tool result, so each view keeps only the fields the
model reasons about.
"""

from pydantic import BaseModel

from arrmate.clients.base_arr import QualityProfile, Release, RootFolder
from arrmate.clients.radarr import Movie, MovieLookup, RadarrHistoryRecord
from arrmate.clients.sonarr import (
    Episode,
    Series,
    SeriesLookup,
    SeriesStatistics,
    SonarrHistoryRecord,
)


class LookupMatch(BaseModel):
    title: str
    year: int
    monitored: bool
    id: int | None = None
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    statistics: SeriesStatistics | None = None


class LibraryEntry(BaseModel):
    id: int
    title: str
    monitored: bool
    quality_profile_id: int
    size_on_disk: int | None = None
    has_file: bool | None = None


class EpisodeRow(BaseModel):
    id: int
    season: int
    episode: int
    title: str
    has_file: bool
    monitored: bool
    air_date: str | None = None


class MissingEpisode(BaseModel):
    episode_id: int
    season: int
    episode: int
    series_title: str | None = None
    air_date: str | None = None


class HistoryRow(BaseModel):
    event_type: str
    date: str
    quality: str
    message: str | None = None
    download_id: str | None = None
    episode_title: str | None = None


class AddOptions(BaseModel):
    profiles: list[QualityProfile]
    root_folders: list[RootFolder]


class Removed(BaseModel):
    item_id: int
    files_deleted: bool


class ReleaseRow(BaseModel):
    index: int
    title: str
    indexer: str
    size: int
    quality: str
    approved: bool
    rejections: list[str]
    seeders: int | None = None


def lookup_match(lookup: SeriesLookup | MovieLookup) -> LookupMatch:
    match lookup:
        case SeriesLookup():
            return LookupMatch(
                id=lookup.id,
                title=lookup.title,
                year=lookup.year,
                monitored=lookup.monitored,
                tvdb_id=lookup.tvdb_id,
                tmdb_id=lookup.tmdb_id or None,
                statistics=lookup.statistics,
            )
        case MovieLookup():
            return LookupMatch(
                id=lookup.id,
                title=lookup.title,
                year=lookup.year,
                monitored=lookup.monitored,
                tmdb_id=lookup.tmdb_id,
            )


def library_entry(item: Series | Movie) -> LibraryEntry:
    match item:
        case Series():
            return LibraryEntry(
                id=item.id,
                title=item.title,
                monitored=item.monitored,
                quality_profile_id=item.quality_profile_id,
                size_on_disk=item.statistics.size_on_disk if item.statistics else None,
            )
        case Movie():
            return LibraryEntry(
                id=item.id,
                title=item.title,
                monitored=item.monitored,
                quality_profile_id=item.quality_profile_id,
                size_on_disk=item.size_on_disk,
                has_file=item.has_file,
            )


def episode_row(episode: Episode) -> EpisodeRow:
    return EpisodeRow(
        id=episode.id,
        season=episode.season_number,
        episode=episode.episode_number,
        title=episode.title,
        has_file=episode.has_file,
        monitored=episode.monitored,
        air_date=episode.air_date,
    )


def missing_episode(episode: Episode) -> MissingEpisode:
    return MissingEpisode(
        episode_id=episode.id,
        season=episode.season_number,
        episode=episode.episode_number,
        series_title=episode.series.title if episode.series else None,
        air_date=episode.air_date,
    )


def history_row(record: SonarrHistoryRecord | RadarrHistoryRecord) -> HistoryRow:
    episode = record.episode if isinstance(record, SonarrHistoryRecord) else None
    return HistoryRow(
        event_type=record.event_type,
        date=record.date,
        quality=record.quality.quality.name,
        message=record.message,
        download_id=record.download_id,
        episode_title=episode.title if episode else None,
    )


def release_row(index: int, release: Release) -> ReleaseRow:
    return ReleaseRow(
        index=index,
        title=release.title,
        indexer=release.indexer,
        size=release.size,
        quality=release.quality.quality.name,
        approved=release.approved,
        rejections=release.rejections,
        seeders=release.seeders,
    )
