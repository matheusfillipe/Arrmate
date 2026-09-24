"""Intent execution orchestrator."""

import asyncio
import json
import logging
from typing import Any, Literal

import httpx
from pydantic import BaseModel

from arrmate.clients.discovery import ArrClient, get_client_for_media_type
from arrmate.clients.lidarr import LidarrClient
from arrmate.clients.plex import PlexClient
from arrmate.clients.radarr import Movie, RadarrClient
from arrmate.clients.readarr import Author, ReadarrClient
from arrmate.clients.readmeabook import ReadMeABookClient
from arrmate.clients.sonarr import Series, SonarrClient
from arrmate.clients.transcoder import (
    create_job,
    ffmpeg_available,
    run_transcode_job,
    scan_for_transcode,
)
from arrmate.config.settings import settings

from .library_service import add_first_artist, add_first_author, add_first_movie, add_first_series
from .models import ActionType, ExecutionResult, Intent

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()

_HISTORY_EVENT_LABELS = {
    "grabbed": "Grabbed",
    "downloadFolderImported": "Imported",
    "downloadFailed": "Failed",
    "episodeFileDeleted": "Deleted",
    "episodeFileRenamed": "Renamed",
    "downloadIgnored": "Ignored",
}


class QueueRow(BaseModel):
    kind: Literal["tv", "movie"]
    show: str
    episode: str
    title: str
    status: str
    progress: int
    eta: str
    protocol: str
    quality: str


class HistoryRow(BaseModel):
    kind: Literal["tv", "movie"]
    show: str
    episode: str
    title: str
    event: str
    date: str
    quality: str


class WantedRow(BaseModel):
    kind: Literal["tv", "movie"]
    show: str
    episode: str
    title: str
    air_date: str


def _as_data(record: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """A service record as the plain dict ``ExecutionResult.data`` and its template read.

    Lidarr records still arrive as raw dicts.
    """
    return record.model_dump(mode="json") if isinstance(record, BaseModel) else record


class Executor:
    """Executes validated intents against media service APIs."""

    async def execute(self, intent: Intent) -> ExecutionResult:
        """Execute an intent and return the result.

        Args:
            intent: The intent to execute (should be enriched)

        Returns:
            ExecutionResult with success status and details
        """
        try:
            # These actions manage their own clients (bypass Arr routing)
            if intent.action == ActionType.TRANSCODE:
                return await self._execute_transcode(intent)
            if intent.action == ActionType.RATE:
                return await self._execute_rate(intent)
            if intent.action == ActionType.BUTLER:
                return await self._execute_butler(intent)
            if intent.action == ActionType.QUEUE:
                return await self._execute_queue(intent)
            if intent.action == ActionType.HISTORY:
                return await self._execute_history(intent)
            if intent.action == ActionType.WANTED:
                return await self._execute_wanted(intent)

            # Get the appropriate client
            client = get_client_for_media_type(intent.media_type)

            try:
                # Route to appropriate handler based on action
                if intent.action == ActionType.REMOVE or intent.action == ActionType.DELETE:
                    return await self._execute_remove(intent, client)
                if intent.action == ActionType.SEARCH:
                    return await self._execute_search(intent, client)
                if intent.action == ActionType.UPGRADE:
                    return await self._execute_upgrade(intent, client)
                if intent.action == ActionType.ADD:
                    return await self._execute_add(intent, client)
                if intent.action == ActionType.LIST:
                    return await self._execute_list(intent, client)
                if intent.action == ActionType.INFO:
                    return await self._execute_info(intent, client)
                if intent.action == ActionType.MONITOR:
                    return await self._execute_monitor(intent, client, monitored=True)
                if intent.action == ActionType.UNMONITOR:
                    return await self._execute_monitor(intent, client, monitored=False)
                if intent.action == ActionType.RENAME:
                    return await self._execute_rename(intent, client)
                if intent.action == ActionType.RESCAN:
                    return await self._execute_rescan(intent, client)
                return ExecutionResult(
                    success=False,
                    message=f"Action '{intent.action}' not yet implemented",
                )
            finally:
                await client.close()

        except (httpx.HTTPError, KeyError, ValueError) as e:
            return ExecutionResult(
                success=False,
                message=f"Execution failed: {e!s}",
                errors=[str(e)],
            )

    async def _execute_remove(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Execute a remove/delete action."""
        match client:
            case SonarrClient():
                return await self._remove_tv_content(intent, client)
            case RadarrClient():
                return await self._remove_movie(intent, client)
            case LidarrClient():
                return await self._remove_music_content(intent, client)
            case ReadarrClient():
                return await self._remove_book_content(intent, client)

    async def _remove_tv_content(self, intent: Intent, client: SonarrClient) -> ExecutionResult:
        """Remove TV show episodes or entire series.

        Args:
            intent: Intent with series/episode info
            client: Sonarr client

        Returns:
            Execution result
        """
        if not intent.series_id:
            return ExecutionResult(
                success=False,
                message=f"Could not find series '{intent.title}' in library",
            )

        # If specific episodes are mentioned, delete those files
        if intent.episodes and intent.season is not None:
            # Get all episodes for the series
            all_episodes = await client.get_episodes(intent.series_id, season_number=intent.season)

            target_episodes = [ep for ep in all_episodes if ep.episode_number in intent.episodes]

            if not target_episodes:
                return ExecutionResult(
                    success=False,
                    message=f"Could not find episodes {intent.episodes} in season {intent.season}",
                )

            file_ids = [ep.episode_file_id for ep in target_episodes if ep.episode_file_id]

            if not file_ids:
                return ExecutionResult(
                    success=False,
                    message=f"Episodes {intent.episodes} have no files to delete",
                )

            # Delete the files
            deleted_count = await client.delete_episode_files(file_ids)

            return ExecutionResult(
                success=True,
                message=(
                    f"Removed {deleted_count} episode file(s) from "
                    f"{intent.title} Season {intent.season}"
                ),
                data={"deleted_count": deleted_count, "file_ids": file_ids},
            )

        # If season is mentioned but no episodes, delete entire season
        if intent.season is not None:
            all_episodes = await client.get_episodes(intent.series_id, season_number=intent.season)

            file_ids = [ep.episode_file_id for ep in all_episodes if ep.episode_file_id]

            if not file_ids:
                return ExecutionResult(
                    success=False,
                    message=f"Season {intent.season} has no files to delete",
                )

            deleted_count = await client.delete_episode_files(file_ids)

            return ExecutionResult(
                success=True,
                message=(
                    f"Removed {deleted_count} episode file(s) from "
                    f"{intent.title} Season {intent.season}"
                ),
                data={"deleted_count": deleted_count},
            )

        # Otherwise, delete entire series
        await client.delete_item(intent.series_id, delete_files=True)
        return ExecutionResult(
            success=True,
            message=f"Removed series '{intent.title}' and all files",
        )

    async def _remove_movie(self, intent: Intent, client: RadarrClient) -> ExecutionResult:
        """Remove a movie.

        Args:
            intent: Intent with movie info
            client: Radarr client

        Returns:
            Execution result
        """
        if not intent.item_id:
            return ExecutionResult(
                success=False,
                message=f"Could not find movie '{intent.title}' in library",
            )

        await client.delete_item(intent.item_id, delete_files=True)

        return ExecutionResult(
            success=True,
            message=f"Removed movie '{intent.title}' and all files",
        )

    async def _remove_music_content(self, intent: Intent, client: LidarrClient) -> ExecutionResult:
        """Remove music (artist).

        Args:
            intent: Intent with artist info
            client: Lidarr client

        Returns:
            Execution result
        """
        if not intent.item_id:
            return ExecutionResult(
                success=False,
                message=f"Could not find artist '{intent.title}' in library",
            )

        await client.delete_item(intent.item_id, delete_files=True)

        return ExecutionResult(
            success=True,
            message=f"Removed artist '{intent.title}' and all files",
        )

    async def _remove_book_content(self, intent: Intent, client: ReadarrClient) -> ExecutionResult:
        """Remove book/audiobook (author).

        Args:
            intent: Intent with author info
            client: Readarr client

        Returns:
            Execution result
        """
        logger.warning("Using deprecated Readarr client for removal")

        if not intent.item_id:
            return ExecutionResult(
                success=False,
                message=f"Could not find author '{intent.title}' in library",
            )

        await client.delete_item(intent.item_id, delete_files=True)

        return ExecutionResult(
            success=True,
            message=f"Removed author '{intent.title}' and all files",
        )

    async def _execute_upgrade(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Execute an upgrade action — search Sonarr/Radarr for a better version.

        Routes to episode-, season-, or series-level search depending on specificity.
        """
        if isinstance(client, SonarrClient) and intent.series_id:
            sonarr = client
            if intent.episodes and intent.season is not None:
                # Specific episode(s): fetch Sonarr episode IDs and run EpisodeSearch
                all_episodes = await sonarr.get_episodes(
                    intent.series_id, season_number=intent.season
                )
                episode_ids = [ep.id for ep in all_episodes if ep.episode_number in intent.episodes]
                if not episode_ids:
                    ep_str = ", ".join(str(e) for e in intent.episodes)
                    return ExecutionResult(
                        success=False,
                        message=(
                            f"Could not find episode(s) {ep_str} in "
                            f"Season {intent.season} of '{intent.title}'"
                        ),
                    )
                await sonarr.trigger_episode_search(episode_ids)
                ep_str = ", ".join(str(e) for e in intent.episodes)
                return ExecutionResult(
                    success=True,
                    message=f"Triggered search for '{intent.title}' S{intent.season:02d}E{ep_str}",
                    data={"task": "EpisodeSearch"},
                )
            if intent.season is not None:
                # Whole season
                await sonarr.trigger_season_search(intent.series_id, intent.season)
                return ExecutionResult(
                    success=True,
                    message=f"Triggered search for '{intent.title}' Season {intent.season}",
                    data={"task": "SeasonSearch"},
                )
            # Whole series
            await client.trigger_item_search(intent.series_id)
            return ExecutionResult(
                success=True,
                message=f"Triggered search for '{intent.title}'",
                data={"task": "SeriesSearch"},
            )
        if isinstance(client, RadarrClient) and intent.item_id:
            await client.trigger_item_search(intent.item_id)
            return ExecutionResult(
                success=True,
                message=f"Triggered search for '{intent.title}'",
                data={"task": "MovieSearch"},
            )
        return ExecutionResult(
            success=False,
            message=f"Could not find '{intent.title}' in library to upgrade",
        )

    async def _execute_search(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Execute a search action."""
        # For items already in library, trigger a search
        if intent.series_id and intent.media_type == "tv":
            await client.trigger_item_search(intent.series_id)
            return ExecutionResult(
                success=True,
                message=f"Triggered search for '{intent.title}'",
                data={"task": "SeriesSearch"},
            )
        if intent.item_id and intent.media_type == "movie":
            await client.trigger_item_search(intent.item_id)
            return ExecutionResult(
                success=True,
                message=f"Triggered search for '{intent.title}'",
                data={"task": "MovieSearch"},
            )
        if intent.item_id and intent.media_type == "music":
            await client.trigger_item_search(intent.item_id)
            return ExecutionResult(
                success=True,
                message=f"Triggered search for '{intent.title}'",
                data={"task": "ArtistSearch"},
            )
        if intent.item_id and intent.media_type in ("audiobook", "book"):
            await client.trigger_item_search(intent.item_id)
            return ExecutionResult(
                success=True,
                message=f"Triggered search for '{intent.title}'",
                data={"task": "AuthorSearch"},
            )
        if intent.keywords and not intent.title:
            # Topic/thematic search: one match found by several keywords is listed once.
            topic_results: dict[str, dict[str, Any]] = {}
            for kw in intent.keywords[:4]:
                for found in await client.search(kw):
                    data = _as_data(found)
                    topic_results.setdefault(json.dumps(data, sort_keys=True), data)
            topic = ", ".join(intent.keywords[:2])
            return ExecutionResult(
                success=True,
                message=f"Found {len(topic_results)} result(s) for topic '{topic}'",
                data={"results": list(topic_results.values())[:10]},
            )
        # Search external sources by title
        results = await client.search(intent.title or "")
        return ExecutionResult(
            success=True,
            message=f"Found {len(results)} result(s) for '{intent.title}'",
            data={"results": [_as_data(found) for found in results[:5]]},
        )

    async def _execute_add(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Execute an add action."""
        # If the item is already in the library, route to upgrade/search instead
        if intent.series_id and intent.media_type == "tv":
            return await self._execute_upgrade(intent, client)
        if intent.item_id and intent.media_type == "movie":
            return await self._execute_upgrade(intent, client)

        # Prefer ReadMeABook (request workflow) if configured; fall back to Readarr
        if (
            intent.media_type in ("audiobook", "book")
            and settings.readmeabook_url
            and settings.readmeabook_api_key
        ):
            rmab = ReadMeABookClient(settings.readmeabook_url, settings.readmeabook_api_key)
            try:
                results = await rmab.search(intent.title or "")
                if not results:
                    return ExecutionResult(
                        success=False,
                        message=f"Could not find '{intent.title}' in ReadMeABook",
                    )
                book = results[0]
                book_title = book.get("title", intent.title or "")
                author = book.get("author", "")
                asin = book.get("asin", "")

                # Check for duplicate requests
                existing = await rmab.get_requests()
                already = any(
                    r.get("asin") == asin or r.get("title", "").lower() == book_title.lower()
                    for r in existing
                )
                if already:
                    return ExecutionResult(
                        success=False,
                        message=f"'{book_title}' has already been requested",
                    )

                if not asin:
                    return ExecutionResult(
                        success=False,
                        message=f"Could not determine ASIN for '{book_title}'; be more specific",
                    )

                await rmab.create_request(asin=asin, title=book_title, author=author)
                return ExecutionResult(
                    success=True,
                    message=f"Requested '{book_title}' via ReadMeABook",
                )
            finally:
                await rmab.close()

        title = intent.title or ""
        added: Series | Movie | Author | dict[str, Any]
        try:
            match client:
                case SonarrClient():
                    # The intent engine leaves the TVDB id of a lookup match in item_id.
                    term = f"tvdb:{intent.item_id}" if intent.item_id else title
                    added = await add_first_series(client, term)
                case RadarrClient():
                    added = await add_first_movie(client, title)
                case LidarrClient():
                    added = await add_first_artist(client, title)
                case ReadarrClient():
                    added = await add_first_author(client, title)
        except ValueError as not_addable:
            return ExecutionResult(success=False, message=f"Could not add '{title}': {not_addable}")
        except httpx.HTTPError as add_err:
            if "already" in _extract_arr_error(add_err).lower():
                return ExecutionResult(
                    success=False, message=f"'{title}' is already in your library"
                )
            raise
        added_title = (
            str(added.get("artistName", title)) if isinstance(added, dict) else added.title
        )
        return ExecutionResult(
            success=True,
            message=f"Added '{added_title}' to library",
            data=_as_data(added),
        )

    async def _execute_list(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Execute a list action."""
        match client:
            case SonarrClient() | RadarrClient() | ReadarrClient():
                titles = [item.title for item in await client.get_all_items()]
            case LidarrClient():
                titles = [str(a.get("artistName", "Unknown")) for a in await client.get_all_items()]
        noun = {
            "tv": "TV show(s)",
            "movie": "movie(s)",
            "music": "artist(s)",
        }.get(intent.media_type or "", "author(s)")
        return ExecutionResult(
            success=True,
            message=f"Found {len(titles)} {noun}",
            data={"titles": titles, "count": len(titles)},
        )

    async def _execute_info(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Execute an info action."""
        if not intent.item_id:
            return ExecutionResult(
                success=False,
                message=f"Could not find '{intent.title}' in library",
            )

        item = await client.get_item(intent.item_id)

        return ExecutionResult(
            success=True,
            message=f"Details for '{intent.title}'",
            data=_as_data(item),
        )

    async def _execute_transcode(self, intent: Intent) -> ExecutionResult:
        """Scan library for non-H265 files and start a background transcode job.

        Args:
            intent: Intent with media_type and optional title filter

        Returns:
            Execution result with job ID and file count
        """
        if not ffmpeg_available():
            return ExecutionResult(
                success=False,
                message="ffmpeg is not installed on the host or in the image.",
            )

        media_type = intent.media_type or "movie"
        title = intent.title

        try:
            files = await scan_for_transcode(media_type=media_type, title=title)
        except (OSError, ValueError) as exc:
            return ExecutionResult(
                success=False,
                message=f"Failed to scan library: {exc}",
                errors=[str(exc)],
            )

        if not files:
            scope = f"'{title}'" if title else f"all {media_type} files"
            return ExecutionResult(
                success=True,
                message=f"No files need transcoding for {scope} — everything is already H.265!",
            )

        job_id = create_job(files, media_type=media_type, title=title)
        task = asyncio.create_task(run_transcode_job(job_id, files))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

        scope = f"'{title}'" if title else f"your {media_type} library"
        total_size = sum(f.get("size", 0) for f in files)
        size_str = _fmt_bytes(total_size)

        return ExecutionResult(
            success=True,
            message=(
                f"Started H.265 transcode job for {scope}: "
                f"{len(files)} file(s) queued (~{size_str} total). "
                f"Job ID: {job_id}. Track progress at /web/transcode"
            ),
            data={
                "job_id": job_id,
                "files_queued": len(files),
                "total_size": size_str,
                "media_type": media_type,
                "title_filter": title,
            },
        )

    async def _execute_rate(self, intent: Intent) -> ExecutionResult:
        """Rate a Plex item using natural language (e.g. 'rate The Matrix 5 stars').

        Searches Plex for the title and applies the star rating.
        """
        if not settings.plex_url or not settings.plex_token:
            return ExecutionResult(success=False, message="Plex is not configured")

        if not intent.title:
            return ExecutionResult(success=False, message="No title specified for rating")

        stars = float((intent.criteria or {}).get("rating", 5))
        client = PlexClient(settings.plex_url, settings.plex_token)
        try:
            hubs = await client.search(intent.title, limit=5)
            rating_key = None
            for hub in hubs:
                for item in hub.get("Metadata", []):
                    if intent.title.lower() in item.get("title", "").lower():
                        rating_key = item.get("ratingKey")
                        break
                if rating_key:
                    break

            if not rating_key:
                return ExecutionResult(
                    success=False,
                    message=f"Could not find '{intent.title}' in Plex",
                )

            ok = await client.rate_item(rating_key, stars)
            if ok:
                return ExecutionResult(
                    success=True,
                    message=f"Rated '{intent.title}' {int(stars)} star(s) in Plex",
                )
            return ExecutionResult(
                success=False, message=f"Failed to rate '{intent.title}' in Plex"
            )
        finally:
            await client.close()

    async def _execute_butler(self, intent: Intent) -> ExecutionResult:
        """Run a Plex Butler maintenance task (e.g. 'clean plex database')."""
        if not settings.plex_url or not settings.plex_token:
            return ExecutionResult(success=False, message="Plex is not configured")

        task = (intent.criteria or {}).get("task", "CleanOldBundles")
        client = PlexClient(settings.plex_url, settings.plex_token)
        try:
            ok = await client.run_butler_task(task)
            if ok:
                return ExecutionResult(
                    success=True,
                    message=f"Started Plex maintenance task: {task}",
                    data={"task": task},
                )
            return ExecutionResult(success=False, message=f"Failed to start Plex task: {task}")
        finally:
            await client.close()

    async def _execute_queue(self, intent: Intent) -> ExecutionResult:
        """Show what is currently downloading in Sonarr and/or Radarr."""
        rows: list[QueueRow] = []
        sources: list[str] = []

        if intent.media_type in ("tv", "tv_show"):
            sonarr = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                for record in (await sonarr.get_queue()).records:
                    rows.append(
                        QueueRow(
                            kind="tv",
                            show=record.series.title if record.series else "",
                            episode=record.episode.label if record.episode else "",
                            title=record.title or "",
                            status=record.status or "",
                            progress=record.progress_percent,
                            eta=(record.estimated_completion_time or "")[:16],
                            protocol=record.protocol or "",
                            quality=record.quality.quality.name if record.quality else "",
                        )
                    )
                sources.append("Sonarr")
            except (httpx.HTTPError, ValueError):
                logger.debug("queue lookup via Sonarr failed", exc_info=True)
            finally:
                await sonarr.close()

        if intent.media_type == "movie" or (
            intent.media_type == "tv" and settings.radarr_url and settings.radarr_api_key
        ):
            radarr = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                for movie_record in (await radarr.get_queue()).records:
                    rows.append(
                        QueueRow(
                            kind="movie",
                            show="",
                            episode="",
                            title=(
                                movie_record.movie.title
                                if movie_record.movie
                                else movie_record.title or ""
                            ),
                            status=movie_record.status or "",
                            progress=movie_record.progress_percent,
                            eta=(movie_record.estimated_completion_time or "")[:16],
                            protocol=movie_record.protocol or "",
                            quality=(
                                movie_record.quality.quality.name if movie_record.quality else ""
                            ),
                        )
                    )
                sources.append("Radarr")
            except (httpx.HTTPError, ValueError):
                logger.debug("queue lookup via Radarr failed", exc_info=True)
            finally:
                await radarr.close()

        if not rows:
            return ExecutionResult(
                success=True,
                message="The download queue is empty.",
                data={"data_type": "queue", "items": [], "total": 0},
            )
        src_str = " + ".join(sources) if sources else "queue"
        return ExecutionResult(
            success=True,
            message=f"{len(rows)} item(s) currently downloading ({src_str})",
            data={
                "data_type": "queue",
                "items": [r.model_dump() for r in rows],
                "total": len(rows),
            },
        )

    async def _execute_history(self, intent: Intent) -> ExecutionResult:
        """Show recent download/import history from Sonarr and/or Radarr."""
        rows: list[HistoryRow] = []

        if intent.media_type != "movie" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                for record in (await sonarr.get_history(page_size=20)).records:
                    rows.append(
                        HistoryRow(
                            kind="tv",
                            show=record.series.title if record.series else "",
                            episode=record.episode.label if record.episode else "",
                            title=record.source_title,
                            event=_HISTORY_EVENT_LABELS.get(record.event_type, record.event_type),
                            date=record.date[:10],
                            quality=record.quality.quality.name,
                        )
                    )
            except (httpx.HTTPError, ValueError):
                logger.debug("history lookup via Sonarr failed", exc_info=True)
            finally:
                await sonarr.close()

        if intent.media_type != "tv" and settings.radarr_url and settings.radarr_api_key:
            radarr = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                for movie_record in (await radarr.get_history(page_size=20)).records:
                    rows.append(
                        HistoryRow(
                            kind="movie",
                            show="",
                            episode="",
                            title=(
                                movie_record.movie.title
                                if movie_record.movie
                                else movie_record.source_title
                            ),
                            event=_HISTORY_EVENT_LABELS.get(
                                movie_record.event_type, movie_record.event_type
                            ),
                            date=movie_record.date[:10],
                            quality=movie_record.quality.quality.name,
                        )
                    )
            except (httpx.HTTPError, ValueError):
                logger.debug("history lookup via Radarr failed", exc_info=True)
            finally:
                await radarr.close()

        rows = sorted(rows, key=lambda row: row.date, reverse=True)[:30]

        if not rows:
            return ExecutionResult(success=True, message="No recent download history found.")
        return ExecutionResult(
            success=True,
            message=f"{len(rows)} recent download event(s)",
            data={
                "data_type": "history",
                "items": [r.model_dump() for r in rows],
                "total": len(rows),
            },
        )

    async def _execute_wanted(self, intent: Intent) -> ExecutionResult:
        """Show monitored media that is missing or below quality cutoff."""
        rows: list[WantedRow] = []

        if intent.media_type != "movie" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                for episode in (await sonarr.get_wanted_missing(page_size=30)).records:
                    rows.append(
                        WantedRow(
                            kind="tv",
                            show=episode.series.title if episode.series else "",
                            episode=episode.label,
                            title=episode.title,
                            air_date=(episode.air_date or "")[:10],
                        )
                    )
            except (httpx.HTTPError, ValueError):
                logger.debug("wanted lookup via Sonarr failed", exc_info=True)
            finally:
                await sonarr.close()

        if intent.media_type != "tv" and settings.radarr_url and settings.radarr_api_key:
            radarr = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                for movie in (await radarr.get_wanted_cutoff(page_size=30)).records:
                    rows.append(
                        WantedRow(
                            kind="movie",
                            show="",
                            episode="",
                            title=movie.title,
                            air_date=(movie.in_cinemas or movie.physical_release or "")[:10],
                        )
                    )
            except (httpx.HTTPError, ValueError):
                logger.debug("wanted lookup via Radarr failed", exc_info=True)
            finally:
                await radarr.close()

        if not rows:
            return ExecutionResult(success=True, message="Nothing is missing — great!")
        return ExecutionResult(
            success=True,
            message=f"{len(rows)} item(s) missing or below quality cutoff",
            data={
                "data_type": "wanted",
                "items": [r.model_dump() for r in rows],
                "total": len(rows),
            },
        )

    async def _execute_monitor(
        self, intent: Intent, client: ArrClient, monitored: bool
    ) -> ExecutionResult:
        """Monitor or unmonitor a series/movie/season."""
        verb = "Monitoring" if monitored else "Unmonitoring"
        if isinstance(client, SonarrClient):
            if not intent.series_id:
                return ExecutionResult(
                    success=False, message=f"Could not find '{intent.title}' in Sonarr"
                )
            if intent.season is not None:
                await client.set_season_monitored(intent.series_id, intent.season, monitored)
                return ExecutionResult(
                    success=True,
                    message=f"{verb} '{intent.title}' Season {intent.season}",
                )
            await client.set_series_monitored(intent.series_id, monitored)
            return ExecutionResult(success=True, message=f"{verb} '{intent.title}'")
        if isinstance(client, RadarrClient):
            if not intent.item_id:
                return ExecutionResult(
                    success=False, message=f"Could not find '{intent.title}' in Radarr"
                )
            await client.set_movie_monitored(intent.item_id, monitored)
            return ExecutionResult(success=True, message=f"{verb} '{intent.title}'")
        return ExecutionResult(
            success=False, message=f"Monitor not supported for {intent.media_type}"
        )

    async def _execute_rename(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Trigger file rename for a series or movie."""
        if isinstance(client, SonarrClient):
            if not intent.series_id:
                return ExecutionResult(
                    success=False, message=f"Could not find '{intent.title}' in Sonarr"
                )
            await client.trigger_rename_series(intent.series_id)
            return ExecutionResult(
                success=True,
                message=f"Rename triggered for '{intent.title}'; naming convention applies",
            )
        if isinstance(client, RadarrClient):
            if not intent.item_id:
                return ExecutionResult(
                    success=False, message=f"Could not find '{intent.title}' in Radarr"
                )
            await client.trigger_rename_movie(intent.item_id)
            return ExecutionResult(
                success=True,
                message=f"Rename triggered for '{intent.title}'",
            )
        return ExecutionResult(
            success=False, message=f"Rename not supported for {intent.media_type}"
        )

    async def _execute_rescan(self, intent: Intent, client: ArrClient) -> ExecutionResult:
        """Trigger disk rescan for a series or movie."""
        if isinstance(client, SonarrClient):
            if not intent.series_id:
                return ExecutionResult(
                    success=False, message=f"Could not find '{intent.title}' in Sonarr"
                )
            await client.rescan_series(intent.series_id)
            return ExecutionResult(
                success=True,
                message=f"Disk rescan started for '{intent.title}'",
            )
        if isinstance(client, RadarrClient):
            if not intent.item_id:
                return ExecutionResult(
                    success=False, message=f"Could not find '{intent.title}' in Radarr"
                )
            await client.rescan_movie(intent.item_id)
            return ExecutionResult(
                success=True,
                message=f"Disk rescan started for '{intent.title}'",
            )
        return ExecutionResult(
            success=False, message=f"Rescan not supported for {intent.media_type}"
        )


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def _extract_arr_error(exc: Exception) -> str:
    """Extract a human-readable message from an Arr API error response."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            body = resp.json()
            if isinstance(body, list) and body:
                return body[0].get("errorMessage") or body[0].get("message") or str(exc)
            if isinstance(body, dict):
                return body.get("message") or body.get("errorMessage") or str(exc)
        except ValueError:
            pass
    return str(exc)
