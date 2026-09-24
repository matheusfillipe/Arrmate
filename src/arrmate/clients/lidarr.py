"""Lidarr API client implementation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .base_arr import BaseArrClient

ArtistMonitor = Literal["all", "future", "missing", "existing", "first", "latest", "none"]
CommandStatus = Literal[
    "queued", "started", "completed", "failed", "aborted", "cancelled", "orphaned"
]


CommandBody = dict[str, str | int | list[int]]


class _LidarrRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class TrackStatistics(_LidarrRecord):
    track_file_count: int = Field(default=0, alias="trackFileCount")
    total_track_count: int = Field(default=0, alias="totalTrackCount")


class _ArtistFields(_LidarrRecord):
    artist_name: str = Field(alias="artistName")
    foreign_artist_id: str = Field(alias="foreignArtistId")
    monitored: bool = False
    disambiguation: str | None = None
    artist_type: str | None = Field(default=None, alias="artistType")


class ArtistLookup(_ArtistFields):
    """A MusicBrainz artist as Lidarr reports it; ``id`` is set once it is in the library."""

    id: int | None = None


class Artist(_ArtistFields):
    id: int
    path: str | None = None
    quality_profile_id: int | None = Field(default=None, alias="qualityProfileId")
    metadata_profile_id: int | None = Field(default=None, alias="metadataProfileId")
    statistics: TrackStatistics | None = None


class Album(_LidarrRecord):
    id: int
    title: str
    artist_id: int = Field(alias="artistId")
    album_type: str | None = Field(default=None, alias="albumType")
    secondary_types: list[str] = Field(default=[], alias="secondaryTypes")
    release_date: str | None = Field(default=None, alias="releaseDate")
    monitored: bool = False
    statistics: TrackStatistics | None = None
    artist: ArtistLookup | None = None


class Track(_LidarrRecord):
    id: int
    title: str
    album_id: int = Field(alias="albumId")
    has_file: bool = Field(default=False, alias="hasFile")


class _PassThroughRecord(_LidarrRecord):
    """A record Lidarr expects back whole, so we keep the fields we do not model."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class QualityName(_PassThroughRecord):
    name: str


class Quality(_PassThroughRecord):
    quality: QualityName


class TrackFile(_LidarrRecord):
    id: int
    album_id: int = Field(alias="albumId")
    path: str
    size: int | None = None
    quality: Quality | None = None


class StatusMessage(_LidarrRecord):
    title: str | None = None
    messages: list[str] = []


class QueueItem(_LidarrRecord):
    id: int
    title: str | None = None
    artist: ArtistLookup | None = None
    album_id: int | None = Field(default=None, alias="albumId")
    download_client: str | None = Field(default=None, alias="downloadClient")
    download_id: str | None = Field(default=None, alias="downloadId")
    output_path: str | None = Field(default=None, alias="outputPath")
    status: str | None = None
    tracked_download_state: str | None = Field(default=None, alias="trackedDownloadState")
    tracked_download_status: str | None = Field(default=None, alias="trackedDownloadStatus")
    size_left: int | None = Field(default=None, alias="sizeleft")
    error_message: str | None = Field(default=None, alias="errorMessage")
    status_messages: list[StatusMessage] | None = Field(default=None, alias="statusMessages")


class Command(_LidarrRecord):
    id: int
    name: str
    status: CommandStatus
    queued: datetime | None = None
    started: datetime | None = None


class Release(_PassThroughRecord):
    """One indexer result from an interactive search, posted back whole to grab it."""

    guid: str
    indexer_id: int = Field(alias="indexerId")
    title: str
    indexer: str | None = None
    protocol: str | None = None
    quality: Quality | None = None
    size: int | None = None
    seeders: int | None = None
    approved: bool = False
    rejections: list[str] = []


class MetadataProfile(_LidarrRecord):
    id: int
    name: str


class _QueuePage(_LidarrRecord):
    records: list[QueueItem] = []


class _AlbumPage(_LidarrRecord):
    records: list[Album] = []


_ARTISTS = TypeAdapter(list[Artist])
_ARTIST_LOOKUPS = TypeAdapter(list[ArtistLookup])
_ALBUMS = TypeAdapter(list[Album])
_TRACKS = TypeAdapter(list[Track])
_TRACK_FILES = TypeAdapter(list[TrackFile])
_COMMANDS = TypeAdapter(list[Command])
_RELEASES = TypeAdapter(list[Release])
_METADATA_PROFILES = TypeAdapter(list[MetadataProfile])


class LidarrClient(BaseArrClient):
    """Client for the Lidarr v1 API (Music)."""

    entity = "artist"
    api_prefix = "api/v1"
    search_command = "ArtistSearch"

    async def get_artists(self) -> list[Artist]:
        return _ARTISTS.validate_python(await self._get(f"{self.api_prefix}/artist"))

    async def lookup_artists(self, term: str) -> list[ArtistLookup]:
        """Artists MusicBrainz knows by this name, library ones carrying their ``id``."""
        found = await self._get(f"{self.api_prefix}/artist/lookup", params={"term": term})
        return _ARTIST_LOOKUPS.validate_python(found)

    async def add_artist(
        self,
        foreign_artist_id: str,
        artist_name: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing: bool = True,
        monitor: ArtistMonitor = "all",
    ) -> Artist:
        """Add a new artist; ``monitor`` picks which existing albums start monitored."""
        data = {
            "foreignArtistId": foreign_artist_id,
            "artistName": artist_name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"monitor": monitor, "searchForMissingAlbums": search_for_missing},
        }
        return Artist.model_validate(await self._post(f"{self.api_prefix}/artist", data=data))

    async def set_artist_monitored(self, artist_id: int, monitored: bool) -> Artist:
        artist = await self._get(f"{self.api_prefix}/artist/{artist_id}")
        artist["monitored"] = monitored
        updated = await self._put(f"{self.api_prefix}/artist/{artist_id}", data=artist)
        return Artist.model_validate(updated)

    async def get_albums(self, artist_id: int) -> list[Album]:
        albums = await self._get(f"{self.api_prefix}/album", params={"artistId": artist_id})
        return _ALBUMS.validate_python(albums)

    async def get_album(self, album_id: int) -> Album:
        return Album.model_validate(await self._get(f"{self.api_prefix}/album/{album_id}"))

    async def get_tracks(self, album_id: int) -> list[Track]:
        tracks = await self._get(f"{self.api_prefix}/track", params={"albumId": album_id})
        return _TRACKS.validate_python(tracks)

    async def get_artist_tracks(self, artist_id: int) -> list[Track]:
        """Every track across an artist's albums."""
        tracks = await self._get(f"{self.api_prefix}/track", params={"artistId": artist_id})
        return _TRACKS.validate_python(tracks)

    async def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> None:
        await self._put(
            f"{self.api_prefix}/album/monitor",
            data={"albumIds": album_ids, "monitored": monitored},
        )

    async def _command(self, body: CommandBody) -> Command:
        return Command.model_validate(await self._post(f"{self.api_prefix}/command", data=body))

    async def import_download(self, path: str, download_id: str) -> Command:
        """Import one finished download now."""
        return await self._command(
            {
                "name": "DownloadedAlbumsScan",
                "path": path,
                "downloadClientId": download_id,
                "importMode": "auto",
            }
        )

    async def refresh_artist(self, artist_id: int) -> Command:
        """Re-read an artist's albums and tracks from metadata."""
        return await self._command({"name": "RefreshArtist", "artistId": artist_id})

    async def trigger_album_search(self, album_ids: list[int]) -> Command:
        return await self._command({"name": "AlbumSearch", "albumIds": album_ids})

    async def get_commands(self) -> list[Command]:
        return _COMMANDS.validate_python(await self._get(f"{self.api_prefix}/command"))

    async def get_command(self, command_id: int) -> Command:
        return Command.model_validate(await self._get(f"{self.api_prefix}/command/{command_id}"))

    async def interactive_search_album(self, album_id: int) -> list[Release]:
        """Search every indexer for releases of one album, rejected ones included."""
        releases = await self._get_with_timeout(
            f"{self.api_prefix}/release", params={"albumId": album_id}
        )
        return _RELEASES.validate_python(releases)

    async def grab_release(self, release: Release) -> Release:
        queued = await self._post(
            f"{self.api_prefix}/release", data=release.model_dump(by_alias=True, exclude_unset=True)
        )
        return Release.model_validate(queued)

    async def get_queue(self, page_size: int = 1000) -> list[QueueItem]:
        """The download queue, including items Lidarr could not match to an artist."""
        page = await self._get(
            f"{self.api_prefix}/queue",
            params={
                "pageSize": page_size,
                "includeUnknownArtistItems": "true",
                "includeArtist": "true",
            },
        )
        return _QueuePage.model_validate(page).records

    async def remove_queue_item(
        self, queue_id: int, remove_from_client: bool, blocklist: bool
    ) -> bool:
        """Drop a queue item, optionally deleting it from the client and blocklisting it."""
        await self._delete(
            f"{self.api_prefix}/queue/{queue_id}"
            f"?removeFromClient={str(remove_from_client).lower()}"
            f"&blocklist={str(blocklist).lower()}&skipRedownload=false"
        )
        return True

    async def get_wanted_missing(self, page_size: int = 200) -> list[Album]:
        """Monitored albums with no files."""
        page = await self._get(
            f"{self.api_prefix}/wanted/missing",
            params={"pageSize": page_size, "includeArtist": "true"},
        )
        return _AlbumPage.model_validate(page).records

    async def get_track_files(self, artist_id: int) -> list[TrackFile]:
        files = await self._get(f"{self.api_prefix}/trackfile", params={"artistId": artist_id})
        return _TRACK_FILES.validate_python(files)

    async def delete_track_file(self, file_id: int) -> bool:
        await self._delete(f"{self.api_prefix}/trackfile/{file_id}")
        return True

    async def get_metadata_profiles(self) -> list[MetadataProfile]:
        profiles = await self._get(f"{self.api_prefix}/metadataprofile")
        return _METADATA_PROFILES.validate_python(profiles)
