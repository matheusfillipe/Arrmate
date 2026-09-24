"""Add-media flow shared by the web library page and the agent add_media tool."""

from arrmate.clients.base_arr import QualityProfile, RootFolder
from arrmate.clients.discovery import ArrClient
from arrmate.clients.lidarr import Artist, LidarrClient
from arrmate.clients.radarr import Movie, RadarrClient
from arrmate.clients.readarr import Author, ReadarrClient
from arrmate.clients.sonarr import Series, SonarrClient


async def _first_profile_and_folder(client: ArrClient) -> tuple[QualityProfile, RootFolder]:
    profiles = await client.get_quality_profiles()
    root_folders = await client.get_root_folders()
    if not profiles or not root_folders:
        raise ValueError("no quality profiles or root folders configured in your service")
    return profiles[0], root_folders[0]


async def add_first_series(client: SonarrClient, title: str, monitored: bool = True) -> Series:
    """Add the first Sonarr match for a title with the first profile and root folder.

    Raises ValueError when Sonarr has no match, profile or folder.
    """
    results = await client.search(title)
    if not results:
        raise ValueError(f"no tv match for {title!r}")
    profile, folder = await _first_profile_and_folder(client)
    return await client.add_series(
        results[0].tvdb_id,
        quality_profile_id=profile.id,
        root_folder_path=folder.path,
        monitored=monitored,
    )


async def add_first_movie(client: RadarrClient, title: str, monitored: bool = True) -> Movie:
    """Add the first Radarr match for a title with the first profile and root folder."""
    results = await client.search(title)
    if not results:
        raise ValueError(f"no movie match for {title!r}")
    profile, folder = await _first_profile_and_folder(client)
    return await client.add_movie(
        tmdb_id=results[0].tmdb_id,
        title=results[0].title,
        quality_profile_id=profile.id,
        root_folder_path=folder.path,
        monitored=monitored,
    )


async def add_first_author(client: ReadarrClient, title: str, monitored: bool = True) -> Author:
    """Add the author of the first Readarr match for a title or author name."""
    authors = [result.author for result in await client.search(title) if result.author]
    if not authors:
        raise ValueError(f"no author match for {title!r}")
    profile, folder = await _first_profile_and_folder(client)
    metadata_profiles = await client.get_metadata_profiles()
    return await client.add_author(
        foreign_author_id=authors[0].foreign_author_id,
        author_name=authors[0].author_name,
        quality_profile_id=profile.id,
        metadata_profile_id=metadata_profiles[0].id if metadata_profiles else 1,
        root_folder_path=folder.path,
        monitored=monitored,
    )


async def add_first_artist(client: LidarrClient, title: str, monitored: bool = True) -> Artist:
    """Add the first Lidarr match for an artist name."""
    results = await client.search(title)
    if not results:
        raise ValueError(f"no music match for {title!r}")
    profile, folder = await _first_profile_and_folder(client)
    metadata_profiles = await client.get_metadata_profiles()
    return await client.add_artist(
        foreign_artist_id=results[0].foreign_artist_id,
        artist_name=results[0].artist_name,
        quality_profile_id=profile.id,
        metadata_profile_id=metadata_profiles[0].id if metadata_profiles else 1,
        root_folder_path=folder.path,
        monitored=monitored,
    )


async def add_first_match(
    client: ArrClient, title: str, monitored: bool = True
) -> Series | Movie | Author | Artist:
    """Search by title and add the first match using the first profile and root folder.

    Raises ValueError when the service lacks a match, profiles, or folders.
    """
    match client:
        case SonarrClient():
            return await add_first_series(client, title, monitored)
        case RadarrClient():
            return await add_first_movie(client, title, monitored)
        case ReadarrClient():
            return await add_first_author(client, title, monitored)
        case LidarrClient():
            return await add_first_artist(client, title, monitored)
