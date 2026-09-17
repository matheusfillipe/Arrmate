"""Lidarr API client implementation."""

from typing import Any

from .base_arr import BaseArrClient


class LidarrClient(BaseArrClient):
    """Client for the Lidarr v1 API (Music)."""

    entity = "artist"
    api_prefix = "api/v1"
    search_command = "ArtistSearch"

    async def add_artist(
        self,
        foreign_artist_id: str,
        artist_name: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing: bool = True,
        monitor: str = "all",
    ) -> dict[str, Any]:
        """Add a new artist to the library.

        ``monitor`` picks which existing albums start monitored: 'all', 'future',
        'missing', 'existing', 'first', 'latest' or 'none'.
        """
        data = {
            "foreignArtistId": foreign_artist_id,
            "artistName": artist_name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"monitor": monitor, "searchForMissingAlbums": search_for_missing},
        }
        return await self._post(f"{self.api_prefix}/artist", data=data)

    async def get_albums(self, artist_id: int) -> list[dict[str, Any]]:
        """Get albums for an artist."""
        return await self._get(f"{self.api_prefix}/album", params={"artistId": artist_id})

    async def get_tracks(self, album_id: int) -> list[dict[str, Any]]:
        """Get tracks for an album."""
        return await self._get(f"{self.api_prefix}/track", params={"albumId": album_id})

    async def get_artist_tracks(self, artist_id: int) -> list[dict[str, Any]]:
        """Get every track across an artist's albums, each carrying its albumId and hasFile."""
        return await self._get(f"{self.api_prefix}/track", params={"artistId": artist_id})

    async def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> Any:
        """Monitor or unmonitor albums."""
        return await self._put(
            f"{self.api_prefix}/album/monitor",
            data={"albumIds": album_ids, "monitored": monitored},
        )

    async def refresh_artist(self, artist_id: int) -> dict[str, Any]:
        """Re-read an artist's albums and tracks from metadata; returns the queued command."""
        return await self._post(
            f"{self.api_prefix}/command",
            data={"name": "RefreshArtist", "artistId": artist_id},
        )

    async def get_command(self, command_id: int) -> dict[str, Any]:
        """Get a queued command, whose ``status`` ends at 'completed' or 'failed'."""
        return await self._get(f"{self.api_prefix}/command/{command_id}")

    async def interactive_search_album(self, album_id: int) -> list[dict[str, Any]]:
        """Search every indexer for releases of one album, rejected ones included."""
        return await self._get_with_timeout(
            f"{self.api_prefix}/release", params={"albumId": album_id}
        )

    async def get_queue(self, page_size: int = 1000) -> dict[str, Any]:
        """Get the download queue, including items Lidarr could not match to an artist."""
        return await self._get(
            f"{self.api_prefix}/queue",
            params={"pageSize": page_size, "includeUnknownArtistItems": "true"},
        )

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

    async def get_wanted_missing(self, page_size: int = 200) -> dict[str, Any]:
        """Get monitored albums with no files."""
        return await self._get(
            f"{self.api_prefix}/wanted/missing",
            params={"pageSize": page_size, "includeArtist": "true"},
        )

    async def get_track_files(self, artist_id: int) -> list[dict[str, Any]]:
        """Get track files for an artist."""
        return await self._get(f"{self.api_prefix}/trackfile", params={"artistId": artist_id})

    async def delete_track_file(self, file_id: int) -> bool:
        """Delete a track file."""
        await self._delete(f"{self.api_prefix}/trackfile/{file_id}")
        return True

    async def trigger_album_search(self, album_ids: list[int]) -> dict[str, Any]:
        """Trigger a search for specific albums."""
        return await self._post(
            f"{self.api_prefix}/command",
            data={"name": "AlbumSearch", "albumIds": album_ids},
        )

    async def get_metadata_profiles(self) -> list[dict[str, Any]]:
        """Get available metadata profiles."""
        return await self._get(f"{self.api_prefix}/metadataprofile")
