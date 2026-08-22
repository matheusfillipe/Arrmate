"""Agent tools over the existing service clients.

Every tool returns a compacted JSON string wrapped in explicit data markers:
tool output carries release names and filenames written by strangers, and the
markers plus the system prompt keep the model reading it as data, not as
instructions. Results are trimmed (nulls stripped, arrays truncated with an
explicit marker) so a 2000-series library cannot blow the context window.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic_ai import Agent, RunContext

from arrmate.clients.discovery import discover_services
from arrmate.core.library_service import add_first_match

from .deps import AgentDeps

logger = logging.getLogger(__name__)

#: A whole homelab library has to fit in one result. Cutting below that made the model try to
#: defeat the cap by guessing filters one letter at a time until it hit the tool-call limit,
#: so the cap belongs above a realistic library rather than merely being announced.
_MAX_LIST_ITEMS = 200
_MAX_STR_LEN = 400
_MAX_WAIT_SECONDS = 300

#: Releases from the last search, held here so a grab never depends on the model
#: reproducing the identifier that keys it. Indexer proxy links and guids run to thousands
#: of characters and are truncated on the way out to the model, so one that makes the round
#: trip is a corrupted one: the indexer then answers an error, and a torrent client accepts
#: the dead link and silently discards it, which reads as a successful grab that never
#: downloads anything. Keyed by tool and subject; the last search for a subject wins.
_RELEASE_CACHE: dict[str, list[dict[str, Any]]] = {}


def _cached_release(key: str, index: int, search_tool: str) -> dict[str, Any] | dict[str, str]:
    """The chosen release from the last search, or an error the model can act on."""
    releases = _RELEASE_CACHE.get(key)
    if releases is None:
        return {"error": "no-search", "detail": f"run {search_tool} for this first"}
    if not 0 <= index < len(releases):
        return {"error": "bad-index", "detail": f"pick 0..{len(releases) - 1}"}
    return releases[index]


_DATA_OPEN = "<<<TOOL_DATA"
_DATA_CLOSE = "TOOL_DATA>>>"


def _compact(value: Any) -> Any:
    """Strip nulls/empties and truncate arrays/strings for model consumption."""
    if isinstance(value, dict):
        cleaned = {k: _compact(v) for k, v in value.items() if v not in (None, "", [], {})}
        return cleaned or None
    if isinstance(value, list):
        items = [_compact(v) for v in value if v not in (None, "", [], {})]
        if len(items) > _MAX_LIST_ITEMS:
            total = len(items)
            items = items[:_MAX_LIST_ITEMS]
            items.append(
                f"...{total - _MAX_LIST_ITEMS} of {total} items omitted. "
                "Report this count to the user. Narrow with a real search term; "
                "repeating the call with guessed terms will not reveal them."
            )
        return items
    if isinstance(value, str) and len(value) > _MAX_STR_LEN:
        return value[:_MAX_STR_LEN] + "…"
    return value


def _wrap(value: Any) -> str:
    return f"{_DATA_OPEN}\n{json.dumps(_compact(value), default=str)}\n{_DATA_CLOSE}"


async def _safe(body: Callable[[], Awaitable[Any]]) -> str:
    """Run a tool body, converting expected errors into model-readable text."""
    try:
        return _wrap(await body())
    except PermissionError as e:
        return _wrap({"error": "permission-denied", "detail": str(e)})
    except ValueError as e:
        return _wrap({"error": "not-configured", "detail": str(e)})
    except (httpx.HTTPError, KeyError, AttributeError, TypeError) as e:
        logger.warning("agent tool failed: %s: %s", type(e).__name__, e)
        return _wrap({"error": "tool-failed", "detail": str(e)[:200]})


def register_tools(agent: Agent[AgentDeps, str]) -> None:
    """Register all chat-agent tools on the given Agent."""

    # ── Read tools ────────────────────────────────────────────────────────────

    @agent.tool
    async def list_services(ctx: RunContext[AgentDeps]) -> str:
        """List configured media services and whether they are currently reachable."""

        async def body() -> Any:
            services = await discover_services()
            return [{"name": name, "available": info.available} for name, info in services.items()]

        return await _safe(body)

    @agent.tool
    async def list_instances(ctx: RunContext[AgentDeps]) -> str:
        """List addressable Sonarr/Radarr instances (primary plus any extras).
        Pass the id as service_id to other tools to target one."""

        async def body() -> Any:
            from arrmate.config import instances

            return instances.list_instances()

        return await _safe(body)

    @agent.tool
    async def library_search(
        ctx: RunContext[AgentDeps], media_type: str, title: str, service_id: str = ""
    ) -> str:
        """Search Sonarr/Radarr for a title. Returns lookup matches with IDs.

        media_type: 'tv' (Sonarr) or 'movie' (Radarr). service_id targets a
        specific instance when several are configured (see list_instances).
        """

        async def body() -> Any:
            def slim(items: list) -> list:
                return [
                    {
                        "id": i.get("id"),
                        "title": i.get("title"),
                        "tvdbId": i.get("tvdbId"),
                        "tmdbId": i.get("tmdbId"),
                        "year": i.get("year"),
                        "monitored": i.get("monitored"),
                        "statistics": i.get("statistics"),
                    }
                    for i in items
                ]

            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as sonarr_client:
                    return slim(await sonarr_client.search(title))
            elif media_type == "movie":
                async with ctx.deps.radarr(service_id) as radarr_client:
                    return slim(await radarr_client.search(title))
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_library(
        ctx: RunContext[AgentDeps], media_type: str, title_filter: str = "", service_id: str = ""
    ) -> str:
        """List items already in the library, optionally filtered by title.

        media_type: 'tv' or 'movie'.
        """

        async def body() -> Any:
            def slim(items: list) -> list:
                out = []
                for i in items:
                    t = (i.get("title") or "").lower()
                    if title_filter and title_filter.lower() not in t:
                        continue
                    out.append(
                        {
                            "id": i.get("id"),
                            "title": i.get("title"),
                            "monitored": i.get("monitored"),
                            "qualityProfileId": i.get("qualityProfileId"),
                            "sizeOnDisk": (i.get("statistics") or {}).get("sizeOnDisk")
                            or i.get("sizeOnDisk"),
                            "hasFile": i.get("hasFile"),
                        }
                    )
                return out

            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as sonarr_client:
                    return slim(await sonarr_client.get_all_items())
            if media_type == "movie":
                async with ctx.deps.radarr(service_id) as radarr_client:
                    return slim(await radarr_client.get_all_items())
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_media(
        ctx: RunContext[AgentDeps], media_type: str, item_id: int, service_id: str = ""
    ) -> str:
        """Get full details for one library item (series with seasons, or movie with file)."""

        async def body() -> Any:
            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as c:
                    return await c.get_item(item_id)
            if media_type == "movie":
                async with ctx.deps.radarr(service_id) as c:
                    return await c.get_item(item_id)
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_episodes(
        ctx: RunContext[AgentDeps], series_id: int, season_number: int = -1
    ) -> str:
        """Get the episode grid of a series (hasFile/monitored per episode).

        Pass season_number=-1 for all seasons.
        """

        async def body() -> Any:
            async with ctx.deps.sonarr() as c:
                eps = await c.get_episodes(series_id, None if season_number < 0 else season_number)
                return [
                    {
                        "id": e.get("id"),
                        "season": e.get("seasonNumber"),
                        "episode": e.get("episodeNumber"),
                        "title": e.get("title"),
                        "hasFile": e.get("hasFile"),
                        "monitored": e.get("monitored"),
                        "airDate": e.get("airDate"),
                    }
                    for e in eps
                ]

        return await _safe(body)

    @agent.tool
    async def get_media_history(
        ctx: RunContext[AgentDeps],
        media_type: str,
        item_id: int,
        episode_id: int = 0,
        service_id: str = "",
    ) -> str:
        """Get grab/import/failure history for a movie or episode.

        For TV pass series episode_id (from get_episodes) when known; the
        generic per-item history otherwise.
        """

        async def body() -> Any:
            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as c:
                    if episode_id:
                        data = await c.get_episode_history(episode_id)
                    else:
                        data = await c.get_history()
                    return [
                        {
                            "eventType": r.get("eventType"),
                            "date": r.get("date"),
                            "quality": (r.get("quality") or {}).get("quality", {}).get("name"),
                            "message": r.get("message"),
                            "downloadId": r.get("downloadId"),
                            "episodeTitle": (r.get("episode") or {}).get("title"),
                        }
                        for r in data.get("records", [])
                    ]
            if media_type == "movie":
                async with ctx.deps.radarr(service_id) as c:
                    data = await c.get_movie_history(item_id)
                    return [
                        {
                            "eventType": r.get("eventType"),
                            "date": r.get("date"),
                            "quality": (r.get("quality") or {}).get("quality", {}).get("name"),
                            "message": r.get("message"),
                            "downloadId": r.get("downloadId"),
                        }
                        for r in data.get("records", [])
                    ]
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_queue(ctx: RunContext[AgentDeps], media_type: str, service_id: str = "") -> str:
        """Get the current download queue from Sonarr or Radarr."""

        async def body() -> Any:
            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as c:
                    return (await c.get_queue()).get("records", [])
            if media_type == "movie":
                async with ctx.deps.radarr(service_id) as c:
                    return (await c.get_queue()).get("records", [])
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_missing_episodes(ctx: RunContext[AgentDeps]) -> str:
        """Get monitored TV episodes that have aired but have no file (wanted/missing)."""

        async def body() -> Any:
            async with ctx.deps.sonarr() as c:
                data = await c.get_wanted_missing()
                return [
                    {
                        "episodeId": r.get("id"),
                        "seriesTitle": (r.get("series") or {}).get("title"),
                        "season": r.get("seasonNumber"),
                        "episode": r.get("episodeNumber"),
                        "airDate": r.get("airDate"),
                    }
                    for r in data.get("records", [])
                ]

        return await _safe(body)

    @agent.tool
    async def interactive_search(
        ctx: RunContext[AgentDeps],
        media_type: str,
        episode_id: int = 0,
        movie_id: int = 0,
        series_id: int = 0,
        season_number: int = -1,
    ) -> str:
        """Run a live interactive indexer search. Can take 30-180 seconds.

        For movies pass movie_id. For a single episode pass episode_id. For a
        whole season pass series_id and season_number. Rejected releases are
        included with their rejection reasons — that is diagnostic signal
        (blocklists, quality refusals), not noise.
        """

        async def body() -> Any:
            def slim(releases: list) -> list:
                _RELEASE_CACHE[f"arr:{media_type}"] = releases
                return [
                    {
                        # guid and indexerId together are what a grab is keyed on. Dropping
                        # indexerId leaves every push_release rejected with a 400.
                        "guid": r.get("guid"),
                        "indexerId": r.get("indexerId"),
                        "title": r.get("title"),
                        "indexer": r.get("indexer"),
                        "size": r.get("size"),
                        "seeders": r.get("seeders"),
                        "quality": (r.get("quality") or {}).get("quality", {}).get("name"),
                        "rejections": r.get("rejections") or [],
                        "approved": r.get("approved"),
                    }
                    for r in releases
                ]

            if media_type == "movie":
                if not movie_id:
                    raise ValueError("movie_id is required for movie searches")
                async with ctx.deps.radarr() as radarr_client:
                    return slim(await radarr_client.interactive_search(movie_id))
            if media_type == "tv":
                async with ctx.deps.sonarr() as sonarr_client:
                    if episode_id:
                        return slim(await sonarr_client.interactive_search_episode(episode_id))
                    if series_id and season_number >= 0:
                        return slim(
                            await sonarr_client.interactive_search_season(series_id, season_number)
                        )
                    raise ValueError("pass episode_id, or series_id and season_number")
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_blocklist(ctx: RunContext[AgentDeps], media_type: str) -> str:
        """Get blocklisted releases from Sonarr or Radarr."""

        async def body() -> Any:
            if media_type == "tv":
                async with ctx.deps.sonarr() as c:
                    return (await c.get_blocklist()).get("records", [])
            if media_type == "movie":
                async with ctx.deps.radarr() as c:
                    return (await c.get_blocklist()).get("records", [])
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def get_download_queue_all(ctx: RunContext[AgentDeps]) -> str:
        """List torrents in qBittorrent with state, progress, and speed."""

        async def body() -> Any:
            async with ctx.deps.qbittorrent() as c:
                return [
                    {
                        "hash": t.get("hash"),
                        "name": t.get("name"),
                        "state": t.get("state"),
                        "progress": t.get("progress"),
                        "size": t.get("size"),
                        "dlspeed": t.get("dlspeed"),
                        "num_seeds": t.get("num_seeds"),
                        "category": t.get("category"),
                    }
                    for t in await c.get_torrents()
                ]

        return await _safe(body)

    @agent.tool
    async def get_download_files(ctx: RunContext[AgentDeps], torrent_hash: str) -> str:
        """List the files inside a torrent. The key malware check: a single
        .exe/.lnk/.scr/.zipx, or a size that does not match the release, means
        a poisoned swarm."""

        async def body() -> Any:
            async with ctx.deps.qbittorrent() as c:
                return await c.get_item_files(torrent_hash)

        return await _safe(body)

    @agent.tool
    async def get_indexer_stats(ctx: RunContext[AgentDeps]) -> str:
        """Get per-indexer grab/query/failure statistics from Prowlarr."""

        async def body() -> Any:
            async with ctx.deps.prowlarr() as client:
                return await client.get_indexer_stats()

        return await _safe(body)

    @agent.tool
    async def wait(ctx: RunContext[AgentDeps], seconds: int, reason: str) -> str:
        """Pause before re-checking something still in progress (a download, an import, a scan).

        Capped at 300 seconds per call. Prefer several short waits over one long one — each
        call is a chance for progress to have moved, and for a user's message to reach you.
        """

        async def body() -> Any:
            delay = max(0, min(seconds, _MAX_WAIT_SECONDS))
            await asyncio.sleep(delay)
            return {"waited_seconds": delay, "reason": reason}

        return await _safe(body)

    @agent.tool
    async def get_add_options(ctx: RunContext[AgentDeps], media_type: str) -> str:
        """Get quality profiles and root folders for adding new media."""

        async def body() -> Any:
            if media_type == "tv":
                async with ctx.deps.sonarr() as c:
                    return {
                        "profiles": [
                            {"id": p.get("id"), "name": p.get("name")}
                            for p in await c.get_quality_profiles()
                        ],
                        "rootFolders": [
                            {"id": r.get("id"), "path": r.get("path")}
                            for r in await c.get_root_folders()
                        ],
                    }
            if media_type == "movie":
                async with ctx.deps.radarr() as c:
                    return {
                        "profiles": [
                            {"id": p.get("id"), "name": p.get("name")}
                            for p in await c.get_quality_profiles()
                        ],
                        "rootFolders": [
                            {"id": r.get("id"), "path": r.get("path")}
                            for r in await c.get_root_folders()
                        ],
                    }
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    # ── Write tools (power_user/admin only) ───────────────────────────────────

    @agent.tool
    async def push_release(ctx: RunContext[AgentDeps], media_type: str, index: int) -> str:
        """Grab one release from the last interactive_search for this media type.

        Pass the index of the chosen result.
        """

        async def body() -> Any:
            ctx.deps.require_write("push_release")
            release = _cached_release(f"arr:{media_type}", index, "interactive_search")
            if "error" in release:
                return release
            if not release.get("indexerId"):
                raise ValueError("that release carries no indexerId, so it cannot be grabbed")
            if media_type == "tv":
                async with ctx.deps.sonarr() as c:
                    return await c.push_release(release)
            if media_type == "movie":
                async with ctx.deps.radarr() as c:
                    return await c.push_release(release)
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def trigger_search(
        ctx: RunContext[AgentDeps],
        media_type: str,
        item_id: int,
        season_number: int = -1,
        episode_ids: list[int] | None = None,
    ) -> str:
        """Tell Sonarr/Radarr to auto-search. TV: item_id is series_id; pass
        season_number for one season, episode_ids for specific episodes."""

        async def body() -> Any:
            ctx.deps.require_write("trigger_search")
            if media_type == "tv":
                async with ctx.deps.sonarr() as c:
                    if episode_ids:
                        return await c.trigger_episode_search(episode_ids)
                    if season_number >= 0:
                        return await c.trigger_season_search(item_id, season_number)
                    return await c.trigger_item_search(item_id)
            if media_type == "movie":
                async with ctx.deps.radarr() as c:
                    return await c.trigger_item_search(item_id)
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def add_media(
        ctx: RunContext[AgentDeps],
        media_type: str,
        title: str,
        monitored: bool = True,
        service_id: str = "",
    ) -> str:
        """Add a new series/movie to the library by title. Uses the first
        quality profile and root folder; searches for missing content
        immediately."""

        async def body() -> Any:
            ctx.deps.require_write("add_media")
            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as c:
                    return await add_first_match(c, media_type, title, monitored=monitored)
            if media_type == "movie":
                async with ctx.deps.radarr(service_id) as c:
                    return await add_first_match(c, media_type, title, monitored=monitored)
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def remove_media(
        ctx: RunContext[AgentDeps],
        media_type: str,
        item_id: int,
        delete_files: bool = False,
        service_id: str = "",
    ) -> str:
        """Remove a series/movie from the library. delete_files also deletes
        the media files — destructive, use deliberately."""

        async def body() -> Any:
            ctx.deps.require_write("remove_media")
            if media_type == "tv":
                async with ctx.deps.sonarr(service_id) as c:
                    ok = await c.delete_item(item_id, delete_files)
            elif media_type == "movie":
                async with ctx.deps.radarr(service_id) as c:
                    ok = await c.delete_item(item_id, delete_files)
            else:
                raise ValueError(f"unsupported media_type: {media_type}")
            return {"removed": ok, "filesDeleted": delete_files}

        return await _safe(body)

    @agent.tool
    async def set_monitored(
        ctx: RunContext[AgentDeps], media_type: str, item_id: int, monitored: bool
    ) -> str:
        """Set monitored on/off for a series or movie."""

        async def body() -> Any:
            ctx.deps.require_write("set_monitored")
            if media_type == "tv":
                async with ctx.deps.sonarr() as c:
                    return await c.set_series_monitored(item_id, monitored)
            if media_type == "movie":
                async with ctx.deps.radarr() as c:
                    return await c.set_movie_monitored(item_id, monitored)
            raise ValueError(f"unsupported media_type: {media_type}")

        return await _safe(body)

    @agent.tool
    async def download_action(
        ctx: RunContext[AgentDeps], action: str, torrent_hash: str, delete_files: bool = False
    ) -> str:
        """Act on a qBittorrent torrent. action: 'delete' (destructive when
        delete_files), 'recheck', or 'reannounce'."""

        async def body() -> Any:
            ctx.deps.require_write(f"download_{action}")
            async with ctx.deps.qbittorrent() as c:
                if action == "delete":
                    ok = await c.delete_torrent(torrent_hash, delete_files)
                    return {"deleted": ok, "filesDeleted": delete_files}
                if action == "recheck":
                    return {"rechecked": await c.recheck_torrent(torrent_hash)}
                if action == "reannounce":
                    return {"reannounced": await c.reannounce_torrent(torrent_hash)}
            raise ValueError(f"unsupported action: {action}")

        return await _safe(body)

    # ── Jellyfin / Jellyseerr ─────────────────────────────────────────────────

    @agent.tool
    async def jellyfin_library(
        ctx: RunContext[AgentDeps], search_term: str = "", item_type: str = "", limit: int = 30
    ) -> str:
        """Search the Jellyfin library. item_type: 'Movie', 'Series', 'Episode',
        or empty for everything."""

        async def body() -> Any:
            async with ctx.deps.jellyfin() as client:
                data = await client.get_items(
                    item_type=item_type, search_term=search_term, limit=limit
                )
                return [
                    {
                        "id": i.get("Id"),
                        "name": i.get("Name"),
                        "type": i.get("Type"),
                        "year": i.get("ProductionYear"),
                        "played": (i.get("UserData") or {}).get("Played"),
                    }
                    for i in data.get("Items", [])
                ]

        return await _safe(body)

    @agent.tool
    async def jellyfin_continue_watching(ctx: RunContext[AgentDeps]) -> str:
        """List what the primary Jellyfin user started and never finished."""

        async def body() -> Any:
            async with ctx.deps.jellyfin() as client:
                users = await client.get_users()
                if not users:
                    raise ValueError("no Jellyfin users found")
                data = await client.get_continue_watching(users[0]["Id"])
                return [
                    {"id": i.get("Id"), "name": i.get("Name"), "type": i.get("Type")}
                    for i in data.get("Items", [])
                ]

        return await _safe(body)

    @agent.tool
    async def jellyfin_scan(ctx: RunContext[AgentDeps]) -> str:
        """Trigger a Jellyfin library scan for new files (after an import)."""

        async def body() -> Any:
            ctx.deps.require_write("jellyfin_scan")
            async with ctx.deps.jellyfin() as client:
                await client.trigger_library_scan()
                return {"scan": "triggered"}

        return await _safe(body)

    @agent.tool
    async def jellyseerr_requests(ctx: RunContext[AgentDeps], status: str = "") -> str:
        """List Jellyseerr requests. status: 'pending', 'approved', 'declined',
        'available', or empty for all."""

        async def body() -> Any:
            async with ctx.deps.jellyseerr() as client:
                data = await client.get_requests(status=status)
                return [
                    {
                        "id": r.get("id"),
                        "title": (r.get("media") or {}).get("title"),
                        "status": r.get("status"),
                        "requestedBy": (r.get("requestedBy") or {}).get("displayName"),
                        "createdAt": r.get("createdAt"),
                    }
                    for r in data.get("results", [])
                ]

        return await _safe(body)

    @agent.tool
    async def jellyseerr_decide(ctx: RunContext[AgentDeps], request_id: int, approve: bool) -> str:
        """Approve or decline a Jellyseerr request (power_user/admin)."""

        async def body() -> Any:
            ctx.deps.require_write("jellyseerr_decide")
            async with ctx.deps.jellyseerr() as client:
                if approve:
                    await client.approve_request(request_id)
                else:
                    await client.decline_request(request_id)
                return {"requestId": request_id, "approved": approve}

        return await _safe(body)

    @agent.tool
    async def jellyseerr_search(ctx: RunContext[AgentDeps], query: str) -> str:
        """TMDB-backed title search via Jellyseerr — resolves a title to a
        tmdbId for add_media without a separate TMDB key."""

        async def body() -> Any:
            async with ctx.deps.jellyseerr() as client:
                data = await client.search_tmdb(query)
                return [
                    {
                        "tmdbId": r.get("id"),
                        "name": r.get("name"),
                        "year": r.get("year"),
                        "mediaType": r.get("mediaType"),
                        "overview": (r.get("overview") or "")[:200],
                    }
                    for r in data.get("results", [])
                ]

        return await _safe(body)

    # ── Listenarr ─────────────────────────────────────────────────────────────

    @agent.tool
    async def listenarr_library(ctx: RunContext[AgentDeps], title_filter: str = "") -> str:
        """List audiobooks already in the Listenarr library, optionally filtered
        by title or author."""

        async def body() -> Any:
            async with ctx.deps.listenarr() as client:
                books = await client.get_all_items()
                needle = title_filter.lower()
                out = []
                for b in books:
                    haystack = f"{b.get('title') or ''} {b.get('author') or ''}".lower()
                    if needle and needle not in haystack:
                        continue
                    out.append(
                        {
                            "id": b.get("id"),
                            "title": b.get("title"),
                            "author": b.get("author"),
                            "narrator": b.get("narrator"),
                            "status": b.get("status"),
                            "monitored": b.get("monitored"),
                        }
                    )
                return out

        return await _safe(body)

    @agent.tool
    async def listenarr_lookup(ctx: RunContext[AgentDeps], query: str) -> str:
        """Search Audible/Audnexus metadata for an audiobook. Use this to find the
        book to hand to listenarr_add; it does not search indexers."""

        async def body() -> Any:
            async with ctx.deps.listenarr() as client:
                results = await client.search_metadata(query, limit=10)
                return [
                    {
                        "asin": r.get("asin"),
                        "title": r.get("title"),
                        "subtitle": r.get("subtitle"),
                        "author": r.get("author") or r.get("authors"),
                        "narrator": r.get("narrator"),
                        "publisher": r.get("publisher"),
                        "releaseDate": r.get("releaseDate"),
                        "runtimeMinutes": r.get("runtimeMinutes") or r.get("lengthMinutes"),
                    }
                    for r in results
                ]

        return await _safe(body)

    @agent.tool
    async def listenarr_search(ctx: RunContext[AgentDeps], query: str, category: str = "") -> str:
        """Search Listenarr's configured indexers for downloadable audiobook
        releases. Returns candidates for listenarr_grab."""

        async def body() -> Any:
            async with ctx.deps.listenarr() as client:
                results = await client.search(query, category=category or None, limit=25)
                return [
                    {
                        "downloadReference": r.get("downloadReference"),
                        "title": r.get("title"),
                        "indexer": r.get("indexer"),
                        "indexerId": r.get("indexerId"),
                        "size": r.get("size"),
                        "seeders": r.get("seeders"),
                        "leechers": r.get("leechers"),
                        "protocol": r.get("protocol"),
                        "ageHours": r.get("ageHours"),
                    }
                    for r in results
                ]

        return await _safe(body)

    @agent.tool
    async def listenarr_add(
        ctx: RunContext[AgentDeps],
        metadata_json: str,
        quality_profile_id: int = 0,
        monitored: bool = True,
        auto_search: bool = False,
    ) -> str:
        """Add an audiobook to the Listenarr library.

        Pass the chosen listenarr_lookup result's JSON object verbatim as
        metadata_json. Leave quality_profile_id at 0 to let Listenarr decide.
        """

        async def body() -> Any:
            ctx.deps.require_write("listenarr_add")
            async with ctx.deps.listenarr() as client:
                return await client.add_book(
                    json.loads(metadata_json),
                    quality_profile_id=quality_profile_id or None,
                    monitored=monitored,
                    auto_search=auto_search,
                )

        return await _safe(body)

    @agent.tool
    async def listenarr_grab(
        ctx: RunContext[AgentDeps], download_reference: str, audiobook_id: int = 0
    ) -> str:
        """Send one release from listenarr_search to a download client.

        download_reference is the downloadReference field of the chosen search
        result. Pass audiobook_id to attach the grab to a library entry.
        """

        async def body() -> Any:
            ctx.deps.require_write("listenarr_grab")
            async with ctx.deps.listenarr() as client:
                return await client.grab_release(
                    download_reference, audiobook_id=audiobook_id or None
                )

        return await _safe(body)

    @agent.tool
    async def listenarr_queue(ctx: RunContext[AgentDeps]) -> str:
        """Get Listenarr's active download queue (progress, state, client)."""

        async def body() -> Any:
            async with ctx.deps.listenarr() as client:
                downloads = await client.get_queue()
                return [
                    {
                        "id": d.get("id"),
                        "title": d.get("title") or d.get("name"),
                        "status": d.get("status"),
                        "progress": d.get("progress"),
                        "downloadClient": d.get("downloadClient"),
                        "errorMessage": d.get("errorMessage"),
                    }
                    for d in downloads
                ]

        return await _safe(body)

    @agent.tool
    async def listenarr_health(ctx: RunContext[AgentDeps]) -> str:
        """Listenarr health: whether its indexers, download clients and metadata
        providers are reachable. Check this first when a grab or import fails."""

        async def body() -> Any:
            async with ctx.deps.listenarr() as client:
                return await client.get_health()

        return await _safe(body)

    # ── Gamearr ───────────────────────────────────────────────────────────────

    @agent.tool
    async def gamearr_library(
        ctx: RunContext[AgentDeps], title_filter: str = "", store: str = ""
    ) -> str:
        """List games already in the Gamearr library, optionally filtered by
        title or store (e.g. 'steam', 'gog')."""

        async def body() -> Any:
            async with ctx.deps.gamearr() as client:
                games = await client.get_games(store=store)
                out = []
                for g in games:
                    if title_filter and title_filter.lower() not in (g.get("title") or "").lower():
                        continue
                    out.append(
                        {
                            "id": g.get("id"),
                            "title": g.get("title"),
                            "platform": g.get("platform"),
                            "store": g.get("store"),
                            "status": g.get("status"),
                            "monitored": g.get("monitored"),
                            "updateAvailable": g.get("updateAvailable"),
                        }
                    )
                return out

        return await _safe(body)

    @agent.tool
    async def gamearr_search(ctx: RunContext[AgentDeps], query: str) -> str:
        """IGDB metadata search for a game title. Use this to find the igdbId
        needed by gamearr_add before adding something new to the library."""

        async def body() -> Any:
            async with ctx.deps.gamearr() as client:
                results = await client.search_games(query)
                return [
                    {
                        "igdbId": r.get("igdbId"),
                        "title": r.get("title"),
                        "year": r.get("year") or r.get("releaseYear"),
                        "existingGameId": r.get("existingGameId"),
                    }
                    for r in results
                ]

        return await _safe(body)

    @agent.tool
    async def gamearr_add(
        ctx: RunContext[AgentDeps],
        igdb_id: int,
        monitored: bool = True,
        store: str = "",
        library_id: int = 0,
        platform: str = "",
    ) -> str:
        """Add a game to the Gamearr library. igdb_id comes from gamearr_search."""

        async def body() -> Any:
            ctx.deps.require_write("gamearr_add")
            async with ctx.deps.gamearr() as client:
                return await client.add_game(
                    igdb_id,
                    monitored=monitored,
                    store=store,
                    library_id=library_id,
                    platform=platform,
                )

        return await _safe(body)

    @agent.tool
    async def gamearr_releases(ctx: RunContext[AgentDeps], game_id: int) -> str:
        """Run a live Prowlarr indexer search for releases of a library game.
        Can take 30-180 seconds. Pass a result's index to gamearr_grab to
        download it."""

        async def body() -> Any:
            async with ctx.deps.gamearr() as client:
                releases = await client.search_releases(game_id)
            _RELEASE_CACHE[f"gamearr:{game_id}"] = releases
            return [
                {
                    "index": i,
                    "title": r.get("title"),
                    "size": r.get("size"),
                    "seeders": r.get("seeders"),
                    "leechers": r.get("leechers"),
                    "indexer": r.get("indexer"),
                    "protocol": r.get("protocol"),
                    "score": r.get("score"),
                }
                for i, r in enumerate(releases)
            ]

        return await _safe(body)

    @agent.tool
    async def gamearr_grab(ctx: RunContext[AgentDeps], game_id: int, index: int) -> str:
        """Grab one release from the last gamearr_releases search for this game.

        Pass the index of the chosen result.
        """

        async def body() -> Any:
            ctx.deps.require_write("gamearr_grab")
            release = _cached_release(f"gamearr:{game_id}", index, "gamearr_releases")
            if "error" in release:
                return release
            async with ctx.deps.gamearr() as client:
                return await client.grab_release(game_id, release)

        return await _safe(body)

    @agent.tool
    async def gamearr_queue(ctx: RunContext[AgentDeps]) -> str:
        """Get Gamearr's active download queue (progress, speed, ETA)."""

        async def body() -> Any:
            async with ctx.deps.gamearr() as client:
                downloads = await client.get_downloads()
                return [
                    {
                        "hash": d.get("hash"),
                        "name": d.get("name"),
                        "status": d.get("status"),
                        "progress": d.get("progress"),
                        "downSpeed": d.get("downSpeed"),
                        "eta": d.get("eta"),
                    }
                    for d in downloads
                ]

        return await _safe(body)
