"""Music tools over Lidarr and Navidrome.

Lidarr is the source of truth for what the music library holds: file names on disk follow
whatever each release was tagged with, so a song is found through Lidarr's track list, never
by matching paths. Navidrome serves the same files and holds the playlists.
"""

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext

from arrmate.agent.deps import AgentDeps
from arrmate.agent.tools import ToolError, _cached_release, _safe
from arrmate.clients.base_arr import CommandStatus
from arrmate.clients.lidarr import (
    Album,
    Artist,
    ArtistMonitor,
    LidarrClient,
    QueueItem,
    Release,
    Track,
)
from arrmate.clients.navidrome import NavidromeClient, ScanStatus, Song
from arrmate.config.settings import settings

_PARENTHETICAL = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_TRACK_WAIT_SECONDS = 180
_TRACK_POLL_SECONDS = 5
_HEALTHY_QUEUE_STATES = {"downloading", "importing"}
_SINGLE_FILE_IMAGE = re.compile(r"image\s*\+|[\(\[]image[\)\]]|\+\s*\.?cue\b", re.IGNORECASE)
_ALBUM_RELEASES: dict[int, list[Release]] = {}

SongStatus = Literal[
    "unparseable",
    "would-add-artist",
    "artist-not-found",
    "song-not-in-lidarr",
    "have",
    "already-queued",
    "would-search",
    "searching",
]


class SongReport(BaseModel):
    request: str
    status: SongStatus
    detail: str | None = None
    added_artist: str | None = None
    album: str | None = None
    album_id: int | None = None
    state: str | None = None


class LibraryDefaults(BaseModel):
    quality_profile_id: int
    metadata_profile_id: int
    root_folder_path: str


class ArtistRow(BaseModel):
    artist_id: int
    name: str
    monitored: bool
    tracks_with_files: int | None = None
    track_count: int | None = None


class AlbumRow(BaseModel):
    album_id: int
    title: str
    type: str | None = None
    secondary_types: list[str] = []
    release_date: str | None = None
    monitored: bool
    tracks_with_files: int | None = None
    track_count: int | None = None


class SongHit(BaseModel):
    title: str
    has_file: bool
    album: AlbumRow | None = None


class ArtistMatch(BaseModel):
    foreign_artist_id: str
    name: str
    disambiguation: str | None = None
    type: str | None = None
    in_library_as: int | None = None


class AddedArtist(BaseModel):
    artist_id: int
    name: str
    monitored: bool


class MonitoredAlbums(BaseModel):
    album_ids: list[int]
    monitored: bool
    searched: bool


class ReleaseRow(BaseModel):
    index: int
    title: str
    indexer: str | None = None
    protocol: str | None = None
    quality: str | None = None
    size: int | None = None
    seeders: int | None = None
    approved: bool
    rejections: list[str] = []
    single_file_image: bool


class AlbumReleases(BaseModel):
    album: str
    artist: str | None = None
    releases: list[ReleaseRow]


class GrabbedRelease(BaseModel):
    grabbed: str
    indexer: str | None = None
    approved: bool


class QueueRow(BaseModel):
    queue_id: int
    title: str | None = None
    artist: str | None = None
    album_id: int | None = None
    client: str | None = None
    status: str | None = None
    state: str | None = None
    health: str | None = None
    size_left: int | None = None
    error: str | None = None
    messages: list[str] = []


class QueueSummary(BaseModel):
    total: int
    by_client_and_state: dict[str, int]
    items: list[QueueRow]


class ImportStarted(BaseModel):
    queue_id: int
    command_id: int | None = None
    error: str | None = None


class StuckCommand(BaseModel):
    name: str
    status: CommandStatus
    since: datetime


class RemovedFromQueue(BaseModel):
    removed: int
    blocklisted: bool


class PlaylistRow(BaseModel):
    playlist_id: str
    name: str
    owner: str | None = None
    public: bool
    songs: int


class PlaylistBuild(BaseModel):
    playlist_id: str
    name: str
    created: bool
    added: int
    already_in_playlist: int
    not_in_navidrome: list[str]


class ScanStart(BaseModel):
    started: bool
    reason: str | None = None
    scan: ScanStatus


def is_single_file_image(release_title: str) -> bool:
    """Release names mark whole-album rips as 'image+.cue', '(image)' or 'APE+CUE'."""
    return bool(_SINGLE_FILE_IMAGE.search(release_title))


def _normalize(text: str) -> str:
    folded = text.casefold().replace("&", "and")
    return _NON_ALNUM.sub(" ", folded).strip()


def _core_title(title: str) -> str:
    """A track title without its bracketed qualifiers, so '(2015 Remaster)' still matches."""
    return _normalize(_PARENTHETICAL.sub("", title)) or _normalize(title)


def _artist_key(name: str) -> str:
    key = _normalize(name)
    return key.removeprefix("the ")


def parse_track_line(line: str) -> tuple[str, str]:
    """Split 'Artist - Title' at its first separator."""
    artist, sep, title = line.partition(" - ")
    if not sep or not artist.strip() or not title.strip():
        raise ValueError(f"expected 'Artist - Title', got {line!r}")
    return artist.strip(), title.strip()


def _title_rank(candidate: str, title: str) -> int | None:
    """0 for the exact spelling, 1 for the same title with other qualifiers, None otherwise."""
    if _core_title(candidate) != _core_title(title):
        return None
    return 0 if _normalize(candidate) == _normalize(title) else 1


def matching_tracks(tracks: list[Track], title: str) -> list[Track]:
    """Tracks whose title is the requested one, exact spellings first."""
    ranked = [(rank, t) for t in tracks if (rank := _title_rank(t.title, title)) is not None]
    return [t for _, t in sorted(ranked, key=lambda pair: pair[0])]


def preferred_album(albums: list[Album]) -> Album:
    """The original studio album among candidates: plain 'Album' type, then earliest."""
    return min(
        albums,
        key=lambda a: (
            a.album_type != "Album",
            bool(a.secondary_types),
            a.release_date or "9999",
        ),
    )


def _album_row(album: Album) -> AlbumRow:
    stats = album.statistics
    return AlbumRow(
        album_id=album.id,
        title=album.title,
        type=album.album_type,
        secondary_types=album.secondary_types,
        release_date=(album.release_date or "")[:10] or None,
        monitored=album.monitored,
        tracks_with_files=stats.track_file_count if stats else None,
        track_count=stats.total_track_count if stats else None,
    )


def _queue_row(item: QueueItem) -> QueueRow:
    messages = [text for status in item.status_messages or [] for text in status.messages]
    return QueueRow(
        queue_id=item.id,
        title=item.title,
        artist=item.artist.artist_name if item.artist else None,
        album_id=item.album_id,
        client=item.download_client,
        status=item.status,
        state=item.tracked_download_state,
        health=item.tracked_download_status,
        size_left=item.size_left,
        error=item.error_message,
        messages=messages[:3],
    )


def _is_problem(item: QueueItem) -> bool:
    return bool(
        item.error_message
        or item.tracked_download_state not in _HEALTHY_QUEUE_STATES
        or item.tracked_download_status not in (None, "ok")
    )


def _most_common_id(ids: list[int | None], fallback: int) -> int:
    counted = Counter(i for i in ids if i is not None).most_common(1)
    return counted[0][0] if counted else fallback


async def _library_defaults(client: LidarrClient) -> LibraryDefaults:
    """The profiles and root folder most of the existing artists use."""
    artists = await client.get_all_items()
    quality = await client.get_quality_profiles()
    metadata = [p for p in await client.get_metadata_profiles() if p.name != "None"]
    roots = await client.get_root_folders()
    if not quality or not metadata or not roots:
        raise ValueError("Lidarr needs a quality profile, metadata profile and root folder")

    root_paths = [r.path.rstrip("/") for r in roots]
    artist_roots = Counter(
        max(holding, key=len)
        for a in artists
        if (holding := [p for p in root_paths if (a.path or "").startswith(p + "/")])
    )
    top_root = artist_roots.most_common(1)
    return LibraryDefaults(
        quality_profile_id=_most_common_id([a.quality_profile_id for a in artists], quality[0].id),
        metadata_profile_id=_most_common_id(
            [a.metadata_profile_id for a in artists], metadata[0].id
        ),
        root_folder_path=top_root[0][0] if top_root else root_paths[0],
    )


async def add_monitored_artist(
    client: LidarrClient, foreign_artist_id: str, name: str, monitor: ArtistMonitor
) -> Artist:
    """Add an artist that stays monitored whichever albums start monitored.

    Lidarr unmonitors the artist itself when added with monitor='none', and an unmonitored
    artist's albums are never picked up by automatic searches or RSS.
    """
    defaults = await _library_defaults(client)
    added = await client.add_artist(
        foreign_artist_id=foreign_artist_id,
        artist_name=name,
        quality_profile_id=defaults.quality_profile_id,
        metadata_profile_id=defaults.metadata_profile_id,
        root_folder_path=defaults.root_folder_path,
        monitored=True,
        search_for_missing=False,
        monitor=monitor,
    )
    if not added.monitored:
        added = await client.set_artist_monitored(added.id, True)
    return added


async def _add_artist(client: LidarrClient, name: str) -> Artist | None:
    """Add the artist a name looks up to, with no albums monitored yet."""
    results = await client.search(name)
    if not results:
        return None
    exact = [r for r in results if _artist_key(r.artist_name) == _artist_key(name)]
    chosen = (exact or results)[0]
    return await add_monitored_artist(client, chosen.foreign_artist_id, chosen.artist_name, "none")


async def _tracks_once_refreshed(client: LidarrClient, artist_id: int) -> list[Track]:
    """An artist added moments ago has no tracks until Lidarr finishes reading its metadata."""
    waited = 0
    while True:
        tracks = await client.get_artist_tracks(artist_id)
        if tracks or waited >= _TRACK_WAIT_SECONDS:
            return tracks
        await asyncio.sleep(_TRACK_POLL_SECONDS)
        waited += _TRACK_POLL_SECONDS


async def ensure_tracks(
    client: LidarrClient, lines: list[str], can_write: bool
) -> list[SongReport]:
    """Resolve each 'Artist - Title' to a Lidarr album and get the missing ones downloading.

    Adds unknown artists, monitors the original studio album that carries each missing song,
    and searches those albums in one command. Without write access it reports the same plan.
    """
    artists = {_artist_key(a.artist_name): a for a in await client.get_all_items()}
    queued_albums = {r.album_id: r.tracked_download_state for r in await client.get_queue()}
    report: list[SongReport] = []
    to_search: list[int] = []
    tracks_by_artist: dict[int, list[Track]] = {}
    albums_by_artist: dict[int, dict[int, Album]] = {}

    for line in lines:
        try:
            artist_name, title = parse_track_line(line)
        except ValueError as e:
            report.append(SongReport(request=line, status="unparseable", detail=str(e)))
            continue

        added_artist = None
        artist = artists.get(_artist_key(artist_name))
        if artist is None:
            if not can_write:
                report.append(SongReport(request=line, status="would-add-artist"))
                continue
            artist = await _add_artist(client, artist_name)
            if artist is None:
                report.append(SongReport(request=line, status="artist-not-found"))
                continue
            artists[_artist_key(artist.artist_name)] = artist
            added_artist = artist.artist_name

        if artist.id not in tracks_by_artist:
            tracks_by_artist[artist.id] = (
                await _tracks_once_refreshed(client, artist.id)
                if added_artist
                else await client.get_artist_tracks(artist.id)
            )
            albums_by_artist[artist.id] = {a.id: a for a in await client.get_albums(artist.id)}
        albums = albums_by_artist[artist.id]
        hits = matching_tracks(tracks_by_artist[artist.id], title)
        if not hits:
            report.append(
                SongReport(
                    request=line,
                    status="song-not-in-lidarr",
                    detail="no album allowed by the artist's metadata profile carries this title",
                    added_artist=added_artist,
                )
            )
            continue

        owned = next((t for t in hits if t.has_file), None)
        if owned:
            owned_album = albums.get(owned.album_id)
            report.append(
                SongReport(
                    request=line,
                    status="have",
                    added_artist=added_artist,
                    album=owned_album.title if owned_album else None,
                )
            )
            continue

        candidate = preferred_album([albums[t.album_id] for t in hits if t.album_id in albums])
        planned = SongReport(
            request=line,
            status="would-search",
            added_artist=added_artist,
            album=candidate.title,
            album_id=candidate.id,
        )
        report.append(planned)
        if candidate.id in queued_albums:
            planned.status = "already-queued"
            planned.state = queued_albums[candidate.id]
            continue
        if not can_write:
            continue
        if not candidate.monitored:
            await client.set_albums_monitored([candidate.id], True)
        if candidate.id not in to_search:
            to_search.append(candidate.id)
        planned.status = "searching"

    if to_search:
        await client.trigger_album_search(to_search)
    return report


def best_song(songs: list[Song], artist: str, title: str) -> Song | None:
    """The Navidrome song for 'artist - title': same title, the artist itself before a
    collaboration it appears in, exact title spelling before a bracketed variant."""
    wanted_artist = _artist_key(artist)

    def artist_rank(song: Song) -> int:
        credited = _artist_key(song.artist or "")
        if credited == wanted_artist:
            return 0
        return 1 if wanted_artist in credited else 2

    ranked = [
        (artist_rank(s), title_rank, s)
        for s in songs
        if (title_rank := _title_rank(s.title, title)) is not None
    ]
    candidates = [r for r in ranked if r[0] < 2]
    return min(candidates, key=lambda r: (r[0], r[1]))[2] if candidates else None


async def build_playlist(
    client: NavidromeClient, name: str, lines: list[str], public: bool, owner: str
) -> PlaylistBuild:
    """Create or extend the playlist called ``name`` with the songs Navidrome has, then hand it
    to ``owner``. Songs already in the playlist are not added twice."""
    found: list[str] = []
    missing: list[str] = []
    for line in lines:
        try:
            artist, title = parse_track_line(line)
        except ValueError:
            missing.append(line)
            continue
        song = best_song(await client.search_songs(_core_title(title)), artist, title)
        if song is None:
            missing.append(line)
        elif song.id not in found:
            found.append(song.id)

    existing = next(
        (p for p in await client.get_playlists() if p.name.casefold() == name.casefold()),
        None,
    )
    playlist_id = existing.id if existing else await client.create_playlist(name)
    present = {t.media_file_id for t in await client.get_playlist_tracks(playlist_id)}
    new_ids = [song_id for song_id in found if song_id not in present]
    added = await client.add_to_playlist(playlist_id, new_ids) if new_ids else 0
    await client.update_playlist(playlist_id, name, public, await client.get_user_id(owner))
    return PlaylistBuild(
        playlist_id=playlist_id,
        name=name,
        created=existing is None,
        added=added,
        already_in_playlist=len(found) - len(new_ids),
        not_in_navidrome=missing,
    )


def register_music_tools(agent: Agent[AgentDeps, str]) -> None:
    """Register the Lidarr music tools on the given Agent."""

    @agent.tool
    async def music_library(ctx: RunContext[AgentDeps], artist_filter: str = "") -> str:
        """List artists in Lidarr with how many of their tracks have files."""

        async def body() -> list[ArtistRow]:
            needle = artist_filter.casefold()
            async with ctx.deps.lidarr() as client:
                artists = await client.get_all_items()
            return [
                ArtistRow(
                    artist_id=a.id,
                    name=a.artist_name,
                    monitored=a.monitored,
                    tracks_with_files=a.statistics.track_file_count if a.statistics else None,
                    track_count=a.statistics.total_track_count if a.statistics else None,
                )
                for a in artists
                if needle in a.artist_name.casefold()
            ]

        return await _safe(body)

    @agent.tool
    async def music_artist_albums(ctx: RunContext[AgentDeps], artist_id: int) -> str:
        """List an artist's albums in Lidarr with type, monitored state and file counts."""

        async def body() -> list[AlbumRow]:
            async with ctx.deps.lidarr() as client:
                return [_album_row(a) for a in await client.get_albums(artist_id)]

        return await _safe(body)

    @agent.tool
    async def music_find_song(ctx: RunContext[AgentDeps], artist_id: int, title: str) -> str:
        """Find a song in an artist's Lidarr tracks: which albums carry it and whether it has a
        file. This is how to answer 'do I have this song', not searching the disk."""

        async def body() -> list[SongHit]:
            async with ctx.deps.lidarr() as client:
                albums = {a.id: a for a in await client.get_albums(artist_id)}
                hits = matching_tracks(await client.get_artist_tracks(artist_id), title)
            return [
                SongHit(
                    title=t.title,
                    has_file=t.has_file,
                    album=_album_row(albums[t.album_id]) if t.album_id in albums else None,
                )
                for t in hits
            ]

        return await _safe(body)

    @agent.tool
    async def music_ensure_songs(ctx: RunContext[AgentDeps], songs: list[str]) -> str:
        """Get a list of songs into the library, each written 'Artist - Title'.

        Reports which songs are already there, adds artists Lidarr does not know, monitors
        the original studio album carrying each missing song and searches all of them.
        Adding a new artist waits for Lidarr to read its albums, so a long list takes minutes.
        Follow up with music_queue, and music_album_releases for any album that finds nothing.
        """

        async def body() -> list[SongReport]:
            async with ctx.deps.lidarr() as client:
                return await ensure_tracks(client, songs, ctx.deps.can_write)

        return await _safe(body)

    @agent.tool
    async def music_lookup_artist(ctx: RunContext[AgentDeps], name: str) -> str:
        """Look an artist up in MusicBrainz through Lidarr, to add with music_add_artist."""

        async def body() -> list[ArtistMatch]:
            async with ctx.deps.lidarr() as client:
                results = await client.search(name)
            return [
                ArtistMatch(
                    foreign_artist_id=r.foreign_artist_id,
                    name=r.artist_name,
                    disambiguation=r.disambiguation,
                    type=r.artist_type,
                    in_library_as=r.id,
                )
                for r in results[:10]
            ]

        return await _safe(body)

    @agent.tool
    async def music_add_artist(
        ctx: RunContext[AgentDeps],
        foreign_artist_id: str,
        name: str,
        monitor: ArtistMonitor = "none",
    ) -> str:
        """Add an artist to Lidarr with the profiles most of the library uses.

        monitor: 'none' (then monitor chosen albums), 'all', 'missing', 'latest' or 'first'.
        Nothing is searched; use music_monitor_albums for that.
        """

        async def body() -> AddedArtist:
            ctx.deps.require_write("music_add_artist")
            async with ctx.deps.lidarr() as client:
                added = await add_monitored_artist(client, foreign_artist_id, name, monitor)
            return AddedArtist(
                artist_id=added.id, name=added.artist_name, monitored=added.monitored
            )

        return await _safe(body)

    @agent.tool
    async def music_monitor_albums(
        ctx: RunContext[AgentDeps],
        album_ids: list[int],
        monitored: bool = True,
        search: bool = True,
    ) -> str:
        """Monitor (or unmonitor) albums, and by default search for the monitored ones."""

        async def body() -> MonitoredAlbums:
            ctx.deps.require_write("music_monitor_albums")
            async with ctx.deps.lidarr() as client:
                await client.set_albums_monitored(album_ids, monitored)
                searched = bool(monitored and search)
                if searched:
                    await client.trigger_album_search(album_ids)
            return MonitoredAlbums(album_ids=album_ids, monitored=monitored, searched=searched)

        return await _safe(body)

    @agent.tool
    async def music_album_releases(ctx: RunContext[AgentDeps], album_id: int) -> str:
        """Search every indexer for one album's releases. Takes 30-180 seconds.

        Rejected releases come back with their reasons. Lidarr's automatic search only takes
        approved ones, so a release rejected for a fixable reason (an edition label, a size
        check) can still be the right one: grab it by index with music_grab_release.
        Never grab one marked single_file_image: it is the whole album as one audio file plus a
        .cue sheet, which Lidarr cannot import and Navidrome cannot split into songs.
        Check the album and artist in the result before grabbing.
        """

        async def body() -> AlbumReleases:
            async with ctx.deps.lidarr() as client:
                album = await client.get_album(album_id)
                releases = await client.interactive_search_album(album_id)
            _ALBUM_RELEASES[album_id] = releases
            return AlbumReleases(
                album=album.title,
                artist=album.artist.artist_name if album.artist else None,
                releases=[
                    ReleaseRow(
                        index=i,
                        title=r.title,
                        indexer=r.indexer,
                        protocol=r.protocol,
                        quality=r.quality.quality.name if r.quality else None,
                        size=r.size,
                        seeders=r.seeders,
                        approved=r.approved,
                        rejections=r.rejections,
                        single_file_image=is_single_file_image(r.title),
                    )
                    for i, r in enumerate(releases)
                ],
            )

        return await _safe(body)

    @agent.tool
    async def music_grab_release(ctx: RunContext[AgentDeps], album_id: int, index: int) -> str:
        """Grab one release from the last music_album_releases search of this album."""

        async def body() -> GrabbedRelease | ToolError:
            ctx.deps.require_write("music_grab_release")
            release = _cached_release(_ALBUM_RELEASES.get(album_id), index, "music_album_releases")
            if isinstance(release, ToolError):
                return release
            async with ctx.deps.lidarr() as client:
                queued = await client.grab_release(release)
            return GrabbedRelease(
                grabbed=release.title, indexer=release.indexer, approved=queued.approved
            )

        return await _safe(body)

    @agent.tool
    async def music_queue(ctx: RunContext[AgentDeps], problems_only: bool = True) -> str:
        """Lidarr's download queue with counts per client and state. problems_only keeps items
        that failed, wait on import, or carry an error message."""

        async def body() -> QueueSummary:
            async with ctx.deps.lidarr() as client:
                records = await client.get_queue()
            counts = Counter(f"{r.download_client}/{r.tracked_download_state}" for r in records)
            shown = [r for r in records if _is_problem(r)] if problems_only else records
            return QueueSummary(
                total=len(records),
                by_client_and_state=dict(counts),
                items=[_queue_row(r) for r in shown],
            )

        return await _safe(body)

    @agent.tool
    async def music_import(ctx: RunContext[AgentDeps], queue_ids: list[int]) -> str:
        """Import finished downloads that sit in the queue waiting on import. This is the only
        way music gets into the library: never copy files into the library folder by hand,
        Lidarr does not track copies and keeps searching for the album."""

        async def body() -> list[ImportStarted]:
            ctx.deps.require_write("music_import")
            async with ctx.deps.lidarr() as client:
                by_id = {r.id: r for r in await client.get_queue()}
                started = []
                for queue_id in queue_ids:
                    record = by_id.get(queue_id)
                    if record is None or not record.output_path:
                        started.append(
                            ImportStarted(queue_id=queue_id, error="not in queue or no files yet")
                        )
                        continue
                    command = await client.import_download(
                        record.output_path, record.download_id or ""
                    )
                    started.append(ImportStarted(queue_id=queue_id, command_id=command.id))
            return started

        return await _safe(body)

    @agent.tool
    async def music_stuck_commands(ctx: RunContext[AgentDeps]) -> str:
        """Lidarr background commands that have been queued or running for over an hour. A
        command stuck in 'started' blocks every later import; restarting Lidarr clears it."""

        async def body() -> list[StuckCommand]:
            cutoff = datetime.now(UTC) - timedelta(hours=1)
            async with ctx.deps.lidarr() as client:
                commands = await client.get_commands()
            stuck = []
            for command in commands:
                since = command.started or command.queued
                if command.status in ("queued", "started") and since and since < cutoff:
                    stuck.append(
                        StuckCommand(name=command.name, status=command.status, since=since)
                    )
            return stuck

        return await _safe(body)

    @agent.tool
    async def music_queue_remove(
        ctx: RunContext[AgentDeps],
        queue_ids: list[int],
        blocklist: bool = True,
        remove_from_client: bool = True,
    ) -> str:
        """Remove queue items. Blocklisting stops Lidarr grabbing the same release again,
        which is what a poisoned or dead release needs."""

        async def body() -> RemovedFromQueue:
            ctx.deps.require_write("music_queue_remove")
            async with ctx.deps.lidarr() as client:
                for queue_id in queue_ids:
                    await client.remove_queue_item(queue_id, remove_from_client, blocklist)
            return RemovedFromQueue(removed=len(queue_ids), blocklisted=blocklist)

        return await _safe(body)

    @agent.tool
    async def navidrome_playlists(ctx: RunContext[AgentDeps]) -> str:
        """List Navidrome playlists with owner, visibility and song count."""

        async def body() -> list[PlaylistRow]:
            async with ctx.deps.navidrome() as client:
                playlists = await client.get_playlists()
            return [
                PlaylistRow(
                    playlist_id=p.id,
                    name=p.name,
                    owner=p.owner_name,
                    public=p.public,
                    songs=p.song_count,
                )
                for p in playlists
            ]

        return await _safe(body)

    @agent.tool
    async def navidrome_build_playlist(
        ctx: RunContext[AgentDeps], name: str, songs: list[str], public: bool = True
    ) -> str:
        """Create a Navidrome playlist, or add to the one with this name, from songs written
        'Artist - Title'. Public playlists show up for every user.

        Songs Navidrome does not have yet come back under not_in_navidrome: get them with
        music_ensure_songs, run navidrome_scan once they import, then call this again with the
        same name to add them.
        """

        async def body() -> PlaylistBuild:
            ctx.deps.require_write("navidrome_build_playlist")
            async with ctx.deps.navidrome() as client:
                owner = settings.navidrome_playlist_owner or client.username
                return await build_playlist(client, name, songs, public, owner)

        return await _safe(body)

    @agent.tool
    async def navidrome_scan(ctx: RunContext[AgentDeps]) -> str:
        """Start a Navidrome scan so newly imported music shows up now. Call it once after
        imports land; a quick scan finishes in seconds. Every call starts a new scan, so
        follow progress with navidrome_scan_status instead of calling this again."""

        async def body() -> ScanStart:
            ctx.deps.require_write("navidrome_scan")
            async with ctx.deps.navidrome() as client:
                status = await client.scan_status()
                if status.scanning:
                    return ScanStart(started=False, reason="a scan is already running", scan=status)
                return ScanStart(started=True, scan=await client.start_scan())

        return await _safe(body)

    @agent.tool
    async def navidrome_scan_status(ctx: RunContext[AgentDeps]) -> str:
        """Whether a Navidrome scan is running, and when the last one finished. Read-only."""

        async def body() -> ScanStatus:
            async with ctx.deps.navidrome() as client:
                return await client.scan_status()

        return await _safe(body)
