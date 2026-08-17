"""Agent tools over the existing service clients.

Every tool returns a compacted JSON string wrapped in explicit data markers:
tool output carries release names and filenames written by strangers, and the
markers plus the system prompt keep the model reading it as data, not as
instructions. Results are trimmed (nulls stripped, arrays truncated with an
explicit marker) so a 2000-series library cannot blow the context window.
"""

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
                return [
                    {
                        "guid": r.get("guid"),
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
    async def push_release(ctx: RunContext[AgentDeps], media_type: str, release_json: str) -> str:
        """Grab one specific release previously returned by interactive_search.

        Pass the release's full JSON object verbatim as release_json.
        """

        async def body() -> Any:
            ctx.deps.require_write("push_release")
            release = json.loads(release_json)
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
