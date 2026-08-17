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
    ) -> dict[str, Any]:
        """Add a new artist to the library."""
        data = {
            "foreignArtistId": foreign_artist_id,
            "artistName": artist_name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"searchForMissingAlbums": search_for_missing},
        }
        return await self._post(f"{self.api_prefix}/artist", data=data)

    async def get_albums(self, artist_id: int) -> list[dict[str, Any]]:
        """Get albums for an artist."""
        return await self._get(f"{self.api_prefix}/album", params={"artistId": artist_id})

    async def get_tracks(self, album_id: int) -> list[dict[str, Any]]:
        """Get tracks for an album."""
        return await self._get(f"{self.api_prefix}/track", params={"albumId": album_id})

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
