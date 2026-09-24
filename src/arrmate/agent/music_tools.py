"""Music tools over Lidarr and Navidrome.

Lidarr is the source of truth for what the music library holds: file names on disk follow
whatever each release was tagged with, so a song is found through Lidarr's track list, never
by matching paths. Navidrome serves the same files and holds the playlists.
"""

import asyncio
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic_ai import Agent, RunContext

from arrmate.agent.deps import AgentDeps
from arrmate.agent.tools import _RELEASE_CACHE, _cached_release, _safe
from arrmate.clients.base_arr import Release
from arrmate.clients.lidarr import LidarrClient
from arrmate.clients.navidrome import NavidromeClient
from arrmate.config.settings import settings

_PARENTHETICAL = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_TRACK_WAIT_SECONDS = 180
_TRACK_POLL_SECONDS = 5
_HEALTHY_QUEUE_STATES = {"downloading", "importing"}
_SINGLE_FILE_IMAGE = re.compile(r"image\s*\+|[\(\[]image[\)\]]|\+\s*\.?cue\b", re.IGNORECASE)


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


def matching_tracks(tracks: list[dict[str, Any]], title: str) -> list[dict[str, Any]]:
    """Tracks whose title is the requested one, exact spellings first."""
    wanted_exact = _normalize(title)
    wanted_core = _core_title(title)
    hits = [t for t in tracks if _core_title(t.get("title") or "") == wanted_core]
    return sorted(hits, key=lambda t: _normalize(t.get("title") or "") != wanted_exact)


def preferred_album(albums: list[dict[str, Any]]) -> dict[str, Any]:
    """The original studio album among candidates: plain 'Album' type, then earliest."""
    return min(
        albums,
        key=lambda a: (
            a.get("albumType") != "Album",
            bool(a.get("secondaryTypes")),
            a.get("releaseDate") or "9999",
        ),
    )


def _slim_album(album: dict[str, Any]) -> dict[str, Any]:
    stats = album.get("statistics") or {}
    return {
        "albumId": album.get("id"),
        "title": album.get("title"),
        "type": album.get("albumType"),
        "secondaryTypes": album.get("secondaryTypes"),
        "releaseDate": (album.get("releaseDate") or "")[:10],
        "monitored": album.get("monitored"),
        "tracksWithFiles": stats.get("trackFileCount"),
        "trackCount": stats.get("totalTrackCount"),
    }


def _slim_queue_item(item: dict[str, Any]) -> dict[str, Any]:
    messages = [
        text for status in item.get("statusMessages") or [] for text in status.get("messages") or []
    ]
    return {
        "queueId": item.get("id"),
        "title": item.get("title"),
        "artist": (item.get("artist") or {}).get("artistName"),
        "albumId": item.get("albumId"),
        "client": item.get("downloadClient"),
        "status": item.get("status"),
        "state": item.get("trackedDownloadState"),
        "health": item.get("trackedDownloadStatus"),
        "sizeLeft": item.get("sizeleft"),
        "error": item.get("errorMessage"),
        "messages": messages[:3],
    }


def _is_problem(item: dict[str, Any]) -> bool:
    return bool(
        item.get("errorMessage")
        or item.get("trackedDownloadState") not in _HEALTHY_QUEUE_STATES
        or item.get("trackedDownloadStatus") not in (None, "ok")
    )


async def _library_defaults(client: LidarrClient) -> dict[str, Any]:
    """The profiles and root folder most of the existing artists use."""
    artists = await client.get_all_items()
    quality = await client.get_quality_profiles()
    metadata = [p for p in await client.get_metadata_profiles() if p.get("name") != "None"]
    roots = await client.get_root_folders()
    if not quality or not metadata or not roots:
        raise ValueError("Lidarr needs a quality profile, metadata profile and root folder")

    def most_common(values: list[Any], fallback: Any) -> Any:
        counted = Counter(v for v in values if v is not None).most_common(1)
        return counted[0][0] if counted else fallback

    root_paths = [r.path.rstrip("/") for r in roots]
    artist_roots = [
        max(
            (p for p in root_paths if (a.get("path") or "").startswith(p + "/")),
            key=len,
            default=None,
        )
        for a in artists
    ]
    return {
        "quality_profile_id": most_common(
            [a.get("qualityProfileId") for a in artists], quality[0].id
        ),
        "metadata_profile_id": most_common(
            [a.get("metadataProfileId") for a in artists], metadata[0]["id"]
        ),
        "root_folder_path": most_common(artist_roots, root_paths[0]),
    }


async def add_monitored_artist(
    client: LidarrClient, foreign_artist_id: str, name: str, monitor: str
) -> dict[str, Any]:
    """Add an artist that stays monitored whichever albums start monitored.

    Lidarr unmonitors the artist itself when added with monitor='none', and an unmonitored
    artist's albums are never picked up by automatic searches or RSS.
    """
    added = await client.add_artist(
        foreign_artist_id=foreign_artist_id,
        artist_name=name,
        monitored=True,
        search_for_missing=False,
        monitor=monitor,
        **await _library_defaults(client),
    )
    if not added.get("monitored"):
        added = await client.set_artist_monitored(added["id"], True)
    return added


async def _add_artist(client: LidarrClient, name: str) -> dict[str, Any] | None:
    """Add the artist a name looks up to, with no albums monitored yet."""
    results = await client.search(name)
    if not results:
        return None
    exact = [r for r in results if _artist_key(r.get("artistName") or "") == _artist_key(name)]
    chosen = (exact or results)[0]
    return await add_monitored_artist(
        client, chosen["foreignArtistId"], chosen["artistName"], "none"
    )


async def _tracks_once_refreshed(client: LidarrClient, artist_id: int) -> list[dict[str, Any]]:
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
) -> list[dict[str, Any]]:
    """Resolve each 'Artist - Title' to a Lidarr album and get the missing ones downloading.

    Adds unknown artists, monitors the original studio album that carries each missing song,
    and searches those albums in one command. Without write access it reports the same plan.
    """
    artists = {_artist_key(a["artistName"]): a for a in await client.get_all_items()}
    queued_albums = {
        r.get("albumId"): r.get("trackedDownloadState")
        for r in (await client.get_queue()).get("records", [])
    }
    report: list[dict[str, Any]] = []
    to_search: list[int] = []
    tracks_by_artist: dict[int, list[dict[str, Any]]] = {}
    albums_by_artist: dict[int, dict[int, dict[str, Any]]] = {}

    for line in lines:
        entry: dict[str, Any] = {"request": line}
        report.append(entry)
        try:
            artist_name, title = parse_track_line(line)
        except ValueError as e:
            entry.update(status="unparseable", detail=str(e))
            continue

        artist = artists.get(_artist_key(artist_name))
        if artist is None:
            if not can_write:
                entry.update(status="would-add-artist")
                continue
            artist = await _add_artist(client, artist_name)
            if artist is None:
                entry.update(status="artist-not-found")
                continue
            artists[_artist_key(artist["artistName"])] = artist
            entry["addedArtist"] = artist["artistName"]

        artist_id = artist["id"]
        if artist_id not in tracks_by_artist:
            tracks_by_artist[artist_id] = (
                await _tracks_once_refreshed(client, artist_id)
                if "addedArtist" in entry
                else await client.get_artist_tracks(artist_id)
            )
            albums_by_artist[artist_id] = {a["id"]: a for a in await client.get_albums(artist_id)}
        albums = albums_by_artist[artist_id]
        hits = matching_tracks(tracks_by_artist[artist_id], title)
        if not hits:
            entry.update(
                status="song-not-in-lidarr",
                detail="no album allowed by the artist's metadata profile carries this title",
            )
            continue

        owned = next((t for t in hits if t.get("hasFile")), None)
        if owned:
            entry.update(status="have", album=albums.get(owned["albumId"], {}).get("title"))
            continue

        candidate = preferred_album([albums[t["albumId"]] for t in hits if t["albumId"] in albums])
        entry.update(album=candidate.get("title"), albumId=candidate["id"])
        if candidate["id"] in queued_albums:
            entry.update(status="already-queued", state=queued_albums[candidate["id"]])
            continue
        if not can_write:
            entry.update(status="would-search")
            continue
        if not candidate.get("monitored"):
            await client.set_albums_monitored([candidate["id"]], True)
        if candidate["id"] not in to_search:
            to_search.append(candidate["id"])
        entry.update(status="searching")

    if to_search:
        await client.trigger_album_search(to_search)
    return report


def best_song(songs: list[dict[str, Any]], artist: str, title: str) -> dict[str, Any] | None:
    """The Navidrome song for 'artist - title': same title, the artist itself before a
    collaboration it appears in, exact title spelling before a bracketed variant."""
    wanted_artist = _artist_key(artist)

    def artist_rank(song: dict[str, Any]) -> int:
        credited = _artist_key(song.get("artist") or "")
        if credited == wanted_artist:
            return 0
        return 1 if wanted_artist in credited else 2

    candidates = [s for s in matching_tracks(songs, title) if artist_rank(s) < 2]
    return min(candidates, key=artist_rank, default=None)


async def build_playlist(
    client: NavidromeClient, name: str, lines: list[str], public: bool, owner: str
) -> dict[str, Any]:
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
        elif song["id"] not in found:
            found.append(song["id"])

    existing = next(
        (
            p
            for p in await client.get_playlists()
            if p.get("name", "").casefold() == name.casefold()
        ),
        None,
    )
    playlist_id = existing["id"] if existing else await client.create_playlist(name)
    present = {t.get("mediaFileId") for t in await client.get_playlist_tracks(playlist_id)}
    new_ids = [song_id for song_id in found if song_id not in present]
    added = await client.add_to_playlist(playlist_id, new_ids) if new_ids else 0
    await client.update_playlist(playlist_id, name, public, await client.get_user_id(owner))
    return {
        "playlistId": playlist_id,
        "name": name,
        "created": existing is None,
        "added": added,
        "alreadyInPlaylist": len(found) - len(new_ids),
        "notInNavidrome": missing,
    }


def register_music_tools(agent: Agent[AgentDeps, str]) -> None:
    """Register the Lidarr music tools on the given Agent."""

    @agent.tool
    async def music_library(ctx: RunContext[AgentDeps], artist_filter: str = "") -> str:
        """List artists in Lidarr with how many of their tracks have files."""

        async def body() -> Any:
            needle = artist_filter.casefold()
            async with ctx.deps.lidarr() as client:
                artists = await client.get_all_items()
            return [
                {
                    "artistId": a.get("id"),
                    "name": a.get("artistName"),
                    "monitored": a.get("monitored"),
                    "tracksWithFiles": (a.get("statistics") or {}).get("trackFileCount"),
                    "trackCount": (a.get("statistics") or {}).get("totalTrackCount"),
                }
                for a in artists
                if needle in (a.get("artistName") or "").casefold()
            ]

        return await _safe(body)

    @agent.tool
    async def music_artist_albums(ctx: RunContext[AgentDeps], artist_id: int) -> str:
        """List an artist's albums in Lidarr with type, monitored state and file counts."""

        async def body() -> Any:
            async with ctx.deps.lidarr() as client:
                return [_slim_album(a) for a in await client.get_albums(artist_id)]

        return await _safe(body)

    @agent.tool
    async def music_find_song(ctx: RunContext[AgentDeps], artist_id: int, title: str) -> str:
        """Find a song in an artist's Lidarr tracks: which albums carry it and whether it has a
        file. This is how to answer 'do I have this song', not searching the disk."""

        async def body() -> Any:
            async with ctx.deps.lidarr() as client:
                albums = {a["id"]: a for a in await client.get_albums(artist_id)}
                hits = matching_tracks(await client.get_artist_tracks(artist_id), title)
            return [
                {
                    "title": t.get("title"),
                    "hasFile": t.get("hasFile"),
                    **_slim_album(albums.get(t["albumId"], {"id": t["albumId"]})),
                }
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

        async def body() -> Any:
            async with ctx.deps.lidarr() as client:
                return await ensure_tracks(client, songs, ctx.deps.can_write)

        return await _safe(body)

    @agent.tool
    async def music_lookup_artist(ctx: RunContext[AgentDeps], name: str) -> str:
        """Look an artist up in MusicBrainz through Lidarr, to add with music_add_artist."""

        async def body() -> Any:
            async with ctx.deps.lidarr() as client:
                results = await client.search(name)
            return [
                {
                    "foreignArtistId": r.get("foreignArtistId"),
                    "name": r.get("artistName"),
                    "disambiguation": r.get("disambiguation"),
                    "type": r.get("artistType"),
                    "inLibraryAs": r.get("id"),
                }
                for r in results[:10]
            ]

        return await _safe(body)

    @agent.tool
    async def music_add_artist(
        ctx: RunContext[AgentDeps], foreign_artist_id: str, name: str, monitor: str = "none"
    ) -> str:
        """Add an artist to Lidarr with the profiles most of the library uses.

        monitor: 'none' (then monitor chosen albums), 'all', 'missing', 'latest' or 'first'.
        Nothing is searched; use music_monitor_albums for that.
        """

        async def body() -> Any:
            ctx.deps.require_write("music_add_artist")
            async with ctx.deps.lidarr() as client:
                added = await add_monitored_artist(client, foreign_artist_id, name, monitor)
            return {
                "artistId": added.get("id"),
                "name": added.get("artistName"),
                "monitored": added.get("monitored"),
            }

        return await _safe(body)

    @agent.tool
    async def music_monitor_albums(
        ctx: RunContext[AgentDeps],
        album_ids: list[int],
        monitored: bool = True,
        search: bool = True,
    ) -> str:
        """Monitor (or unmonitor) albums, and by default search for the monitored ones."""

        async def body() -> Any:
            ctx.deps.require_write("music_monitor_albums")
            async with ctx.deps.lidarr() as client:
                await client.set_albums_monitored(album_ids, monitored)
                searched = bool(monitored and search)
                if searched:
                    await client.trigger_album_search(album_ids)
            return {"albumIds": album_ids, "monitored": monitored, "searched": searched}

        return await _safe(body)

    @agent.tool
    async def music_album_releases(ctx: RunContext[AgentDeps], album_id: int) -> str:
        """Search every indexer for one album's releases. Takes 30-180 seconds.

        Rejected releases come back with their reasons. Lidarr's automatic search only takes
        approved ones, so a release rejected for a fixable reason (an edition label, a size
        check) can still be the right one: grab it by index with music_grab_release.
        Never grab one marked singleFileImage: it is the whole album as one audio file plus a
        .cue sheet, which Lidarr cannot import and Navidrome cannot split into songs.
        Check the album and artist in the result before grabbing.
        """

        async def body() -> Any:
            async with ctx.deps.lidarr() as client:
                album = await client.get_album(album_id)
                releases = await client.interactive_search_album(album_id)
            _RELEASE_CACHE[f"lidarr:{album_id}"] = releases
            return {
                "album": album.get("title"),
                "artist": (album.get("artist") or {}).get("artistName"),
                "releases": [
                    {
                        "index": i,
                        "title": r.get("title"),
                        "indexer": r.get("indexer"),
                        "protocol": r.get("protocol"),
                        "quality": ((r.get("quality") or {}).get("quality") or {}).get("name"),
                        "size": r.get("size"),
                        "seeders": r.get("seeders"),
                        "approved": r.get("approved"),
                        "rejections": r.get("rejections"),
                        "singleFileImage": is_single_file_image(r.get("title") or ""),
                    }
                    for i, r in enumerate(releases)
                ],
            }

        return await _safe(body)

    @agent.tool
    async def music_grab_release(ctx: RunContext[AgentDeps], album_id: int, index: int) -> str:
        """Grab one release from the last music_album_releases search of this album."""

        async def body() -> Any:
            ctx.deps.require_write("music_grab_release")
            release = _cached_release(f"lidarr:{album_id}", index, "music_album_releases")
            if "error" in release:
                return release
            async with ctx.deps.lidarr() as client:
                await client.push_release(Release.model_validate(release))
            return {"grabbed": release.get("title"), "indexer": release.get("indexer")}

        return await _safe(body)

    @agent.tool
    async def music_queue(ctx: RunContext[AgentDeps], problems_only: bool = True) -> str:
        """Lidarr's download queue with counts per client and state. problems_only keeps items
        that failed, wait on import, or carry an error message."""

        async def body() -> Any:
            async with ctx.deps.lidarr() as client:
                records = (await client.get_queue()).get("records", [])
            counts = Counter(
                f"{r.get('downloadClient')}/{r.get('trackedDownloadState')}" for r in records
            )
            shown = [r for r in records if _is_problem(r)] if problems_only else records
            return {
                "total": len(records),
                "byClientAndState": dict(counts),
                "items": [_slim_queue_item(r) for r in shown],
            }

        return await _safe(body)

    @agent.tool
    async def music_import(ctx: RunContext[AgentDeps], queue_ids: list[int]) -> str:
        """Import finished downloads that sit in the queue waiting on import. This is the only
        way music gets into the library: never copy files into the library folder by hand,
        Lidarr does not track copies and keeps searching for the album."""

        async def body() -> Any:
            ctx.deps.require_write("music_import")
            async with ctx.deps.lidarr() as client:
                by_id = {r.get("id"): r for r in (await client.get_queue()).get("records", [])}
                started = []
                for queue_id in queue_ids:
                    record = by_id.get(queue_id)
                    if record is None or not record.get("outputPath"):
                        started.append(
                            {"queueId": queue_id, "error": "not in queue or no files yet"}
                        )
                        continue
                    command = await client.import_download(
                        record["outputPath"], record.get("downloadId") or ""
                    )
                    started.append({"queueId": queue_id, "commandId": command.get("id")})
            return started

        return await _safe(body)

    @agent.tool
    async def music_stuck_commands(ctx: RunContext[AgentDeps]) -> str:
        """Lidarr background commands that have been queued or running for over an hour. A
        command stuck in 'started' blocks every later import; restarting Lidarr clears it."""

        async def body() -> Any:
            cutoff = datetime.now(UTC) - timedelta(hours=1)
            async with ctx.deps.lidarr() as client:
                commands = await client.get_commands()
            stuck = []
            for command in commands:
                since = command.get("started") or command.get("queued")
                if command.get("status") not in ("queued", "started") or not since:
                    continue
                if datetime.fromisoformat(since.replace("Z", "+00:00")) < cutoff:
                    stuck.append(
                        {
                            "name": command.get("name"),
                            "status": command.get("status"),
                            "since": since,
                        }
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

        async def body() -> Any:
            ctx.deps.require_write("music_queue_remove")
            async with ctx.deps.lidarr() as client:
                for queue_id in queue_ids:
                    await client.remove_queue_item(queue_id, remove_from_client, blocklist)
            return {"removed": len(queue_ids), "blocklisted": blocklist}

        return await _safe(body)

    @agent.tool
    async def navidrome_playlists(ctx: RunContext[AgentDeps]) -> str:
        """List Navidrome playlists with owner, visibility and song count."""

        async def body() -> Any:
            async with ctx.deps.navidrome() as client:
                playlists = await client.get_playlists()
            return [
                {
                    "playlistId": p.get("id"),
                    "name": p.get("name"),
                    "owner": p.get("ownerName"),
                    "public": p.get("public"),
                    "songs": p.get("songCount"),
                }
                for p in playlists
            ]

        return await _safe(body)

    @agent.tool
    async def navidrome_build_playlist(
        ctx: RunContext[AgentDeps], name: str, songs: list[str], public: bool = True
    ) -> str:
        """Create a Navidrome playlist, or add to the one with this name, from songs written
        'Artist - Title'. Public playlists show up for every user.

        Songs Navidrome does not have yet come back under notInNavidrome: get them with
        music_ensure_songs, run navidrome_scan once they import, then call this again with the
        same name to add them.
        """

        async def body() -> Any:
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

        async def body() -> Any:
            ctx.deps.require_write("navidrome_scan")
            async with ctx.deps.navidrome() as client:
                status = await client.scan_status()
                if status.get("scanning"):
                    return {"started": False, "reason": "a scan is already running", **status}
                return {"started": True, **await client.start_scan()}

        return await _safe(body)

    @agent.tool
    async def navidrome_scan_status(ctx: RunContext[AgentDeps]) -> str:
        """Whether a Navidrome scan is running, and when the last one finished. Read-only."""

        async def body() -> Any:
            async with ctx.deps.navidrome() as client:
                return await client.scan_status()

        return await _safe(body)
