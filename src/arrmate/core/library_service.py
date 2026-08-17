"""Add-media flow shared by the web library page and the agent add_media tool."""

import logging
from typing import Any, cast

from arrmate.clients.base_arr import BaseArrClient
from arrmate.clients.lidarr import LidarrClient
from arrmate.clients.radarr import RadarrClient
from arrmate.clients.readarr import ReadarrClient
from arrmate.clients.sonarr import SonarrClient
from arrmate.core.models import MediaType

logger = logging.getLogger(__name__)


async def add_first_match(
    client: BaseArrClient,
    media_type: str,
    title: str,
    monitored: bool = True,
) -> dict[str, Any]:
    """Search by title, add the first match using the first profile and root folder.

    Raises ValueError when the service lacks a match, profiles, or folders.
    """
    results = await client.search(title)
    if not results:
        raise ValueError(f"no {media_type} match for {title!r}")

    profiles = await client.get_quality_profiles()
    root_folders = await client.get_root_folders()
    if not profiles or not root_folders:
        raise ValueError("no quality profiles or root folders configured in your service")
    profile_id = profiles[0]["id"]
    root_folder = root_folders[0]["path"]
    item = results[0]

    if media_type == MediaType.TV:
        sonarr = cast(SonarrClient, client)
        tvdb_id = item.get("tvdbId")
        if tvdb_id:
            full_lookup = await sonarr.search(f"tvdb:{tvdb_id}")
            item = full_lookup[0] if full_lookup else item
        return await sonarr.add_series_from_lookup(
            item,
            quality_profile_id=profile_id,
            root_folder_path=root_folder,
            monitored=monitored,
        )

    if media_type == MediaType.MOVIE:
        radarr = cast(RadarrClient, client)
        return await radarr.add_movie(
            tmdb_id=item["tmdbId"],
            title=item["title"],
            quality_profile_id=profile_id,
            root_folder_path=root_folder,
            monitored=monitored,
        )

    if media_type == MediaType.MUSIC:
        lidarr = cast(LidarrClient, client)
        metadata_profiles = await lidarr.get_metadata_profiles()
        metadata_profile_id = metadata_profiles[0]["id"] if metadata_profiles else 1
        return await lidarr.add_artist(
            foreign_artist_id=item["foreignArtistId"],
            artist_name=item.get("artistName", title),
            quality_profile_id=profile_id,
            metadata_profile_id=metadata_profile_id,
            root_folder_path=root_folder,
            monitored=monitored,
        )

    if media_type in (MediaType.AUDIOBOOK, MediaType.BOOK):
        readarr = cast(ReadarrClient, client)
        metadata_profiles = await readarr.get_metadata_profiles()
        metadata_profile_id = metadata_profiles[0]["id"] if metadata_profiles else 1
        return await readarr.add_author(
            foreign_author_id=item["foreignAuthorId"],
            author_name=item.get("authorName", title),
            quality_profile_id=profile_id,
            metadata_profile_id=metadata_profile_id,
            root_folder_path=root_folder,
            monitored=monitored,
        )

    raise ValueError(f"unsupported media type: {media_type}")
