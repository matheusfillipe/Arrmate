"""Shared state and helpers for the web route modules."""

import asyncio
import contextlib
import logging
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from arrmate.auth import auth_manager, user_db
from arrmate.auth.dependencies import (
    AuthRedirectException,
    get_current_user,
    require_admin,
    require_any_auth,
    require_auth,
    require_power_user,
    safe_next_url,
)
from arrmate.auth.notifications import notify_request_resolved, notify_request_submitted
from arrmate.auth.plex_sso import (
    build_plex_auth_url,
    clear_plex_state_cookie,
    get_plex_friend_uuids,
    get_plex_state,
    get_plex_user,
    plex_client_id,
    request_pin,
    set_plex_state_cookie,
    validate_pin,
)
from arrmate.auth.rate_limit import login_limiter
from arrmate.auth.session import (
    clear_session_cookie,
    create_session_token,
    set_session_cookie,
)
from arrmate.clients.discovery import discover_services, get_client_for_media_type
from arrmate.clients.lastfm import LastFMClient
from arrmate.clients.lidarr import LidarrClient
from arrmate.clients.openlibrary import OpenLibraryClient
from arrmate.clients.plex import PlexClient
from arrmate.clients.plex_tv import PlexTVClient
from arrmate.clients.radarr import RadarrClient
from arrmate.clients.readmeabook import ReadMeABookClient
from arrmate.clients.sonarr import SonarrClient
from arrmate.clients.tmdb import TMDBClient
from arrmate.clients.transcoder import cancel_job, get_all_jobs, get_job
from arrmate.config.service_config import save_service_config
from arrmate.config.settings import settings
from arrmate.core.command_parser import CommandParser
from arrmate.core.executor import Executor
from arrmate.core.intent_engine import IntentEngine
from arrmate.core.models import DESTRUCTIVE_ACTIONS, USER_BLOCKED_ACTIONS, ActionType
from arrmate.llm.base import ConversationalReply

__all__ = [
    "BUTLER_TASKS",
    "DESTRUCTIVE_ACTIONS",
    "USER_BLOCKED_ACTIONS",
    "_AUDIOBOOK_CATEGORIES",
    "_BOOK_CATEGORIES",
    "_DISCOVER_CATEGORIES",
    "_MUSIC_CATEGORIES",
    "_WIZARD_STEPS",
    "APIRouter",
    "ActionType",
    "AuthRedirectException",
    "CommandParser",
    "ConversationalReply",
    "Depends",
    "Executor",
    "Form",
    "HTMLResponse",
    "HTTPException",
    "IntentEngine",
    "Jinja2Templates",
    "LastFMClient",
    "LidarrClient",
    "OpenLibraryClient",
    "Optional",
    "Path",
    "PlexClient",
    "PlexTVClient",
    "Query",
    "RadarrClient",
    "ReadMeABookClient",
    "RedirectResponse",
    "Request",
    "Response",
    "SonarrClient",
    "TMDBClient",
    "_base_ctx",
    "_format_size",
    "_get_movie_count",
    "_get_tv_count",
    "_lastfm_client",
    "_plex_client",
    "_plex_client_for_user",
    "_plex_thumb_url",
    "_plex_tv_client",
    "_prowlarr_client",
    "_timestamp_to_relative",
    "_tmdb_client",
    "asyncio",
    "auth_manager",
    "auth_router",
    "build_plex_auth_url",
    "cancel_job",
    "clear_plex_state_cookie",
    "clear_session_cookie",
    "contextlib",
    "create_session_token",
    "discover_services",
    "engine",
    "executor",
    "get_all_jobs",
    "get_client_for_media_type",
    "get_current_user",
    "get_engine",
    "get_executor",
    "get_job",
    "get_parser",
    "get_plex_friend_uuids",
    "get_plex_state",
    "get_plex_user",
    "logger",
    "logging",
    "login_limiter",
    "notify_request_resolved",
    "notify_request_submitted",
    "parser",
    "plex_client_id",
    "quote_plus",
    "request_pin",
    "require_admin",
    "require_any_auth",
    "require_auth",
    "require_power_user",
    "reset_parser",
    "router",
    "safe_next_url",
    "save_service_config",
    "set_plex_state_cookie",
    "set_session_cookie",
    "settings",
    "sqlite3",
    "templates",
    "templates_dir",
    "user_db",
    "validate_pin",
]


"""Web routes for Arrmate HTMX interface."""


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/web", tags=["web"], dependencies=[Depends(require_auth)])


auth_router = APIRouter(prefix="/web", tags=["auth"])


templates_dir = Path(__file__).resolve().parent.parent / "templates"


templates = Jinja2Templates(directory=str(templates_dir))


templates.env.globals["auth_manager"] = auth_manager


templates.env.globals["settings"] = settings


def _timestamp_to_relative(ts: int) -> str:
    """Convert a Unix timestamp to a human-readable relative string."""
    import time

    delta = int(time.time()) - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        m = delta // 60
        return f"{m}m ago"
    if delta < 86400:
        h = delta // 3600
        return f"{h}h ago"
    if delta < 604800:
        d = delta // 86400
        return f"{d}d ago"
    if delta < 2592000:
        w = delta // 604800
        return f"{w}w ago"
    mo = delta // 2592000
    return f"{mo}mo ago"


templates.env.filters["timestamp_to_relative"] = _timestamp_to_relative


parser: CommandParser | None = None


engine: IntentEngine | None = None


executor: Executor | None = None


_USER_BLOCKED_ACTIONS = {ActionType.REMOVE, ActionType.DELETE}


async def get_parser() -> CommandParser:
    """Get or create command parser, with service-aware prompt on first init."""
    global parser
    if parser is None:
        services = await discover_services()
        available = [name for name, info in services.items() if info.available]
        parser = CommandParser(available_services=available or None)
    return parser


def get_engine() -> IntentEngine:
    """Get or create intent engine."""
    global engine
    if engine is None:
        engine = IntentEngine()
    return engine


def get_executor() -> Executor:
    """Get or create executor."""
    global executor
    if executor is None:
        executor = Executor()
    return executor


def reset_parser() -> None:
    """Reset the cached parser so the next command re-initialises it with current services."""
    global parser
    parser = None


def _base_ctx(request: Request) -> dict:
    """Base template context: current user + unread notification count."""
    user = get_current_user(request)
    unread = 0
    if user:
        uid = user.get("user_id") or user.get("id", "")
        if uid and uid != "legacy":
            with contextlib.suppress(httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                unread = user_db.get_unread_count(uid)
            # Merge must_change_password from DB into session dict
            try:
                db_user = user_db.get_user_by_id(uid)
                if db_user:
                    user = {**user, "must_change_password": db_user.get("must_change_password", 0)}
            except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                pass
    return {"current_user": user, "unread_count": unread}


async def _get_tv_count() -> int | None:
    """Fetch number of series from Sonarr, returns None if unavailable."""
    if not settings.sonarr_url or not settings.sonarr_api_key:
        return None
    client = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
    try:
        series = await client.get_all_items()
        return len(series)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        return None
    finally:
        await client.close()


async def _get_movie_count() -> int | None:
    """Fetch number of movies from Radarr, returns None if unavailable."""
    if not settings.radarr_url or not settings.radarr_api_key:
        return None
    client = RadarrClient(settings.radarr_url, settings.radarr_api_key)
    try:
        movies = await client.get_all_items()
        return len(movies)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        return None
    finally:
        await client.close()


_WIZARD_STEPS = [
    ("welcome", "Welcome"),
    ("llm", "AI / LLM"),
    ("media", "Media Services"),
    ("downloads", "Download Clients"),
    ("extras", "Extras"),
    ("done", "Done"),
]


BUTLER_TASKS = [
    {"name": "CleanOldBundles", "label": "Clean Old Bundles", "desc": "Remove unused bundle data"},
    {
        "name": "CleanOldCacheFiles",
        "label": "Clean Cache Files",
        "desc": "Delete stale cached files",
    },
    {"name": "BackupDatabase", "label": "Backup Database", "desc": "Back up the Plex database"},
    {
        "name": "DeepMediaAnalysis",
        "label": "Deep Media Analysis",
        "desc": "Re-analyse loudness & bitrate",
    },
    {
        "name": "RefreshLocalMedia",
        "label": "Refresh Local Media",
        "desc": "Scan for local metadata/artwork",
    },
    {
        "name": "SearchForSubtitles",
        "label": "Search for Subtitles",
        "desc": "Find missing subtitle files",
    },
    {"name": "GenerateAutoTags", "label": "Generate Auto Tags", "desc": "Auto-tag music files"},
    {
        "name": "UpgradeMediaAnalysis",
        "label": "Upgrade Media Analysis",
        "desc": "Update media analysis data",
    },
    {
        "name": "GenerateChapterImageThumbnails",
        "label": "Chapter Thumbnails",
        "desc": "Generate chapter image thumbnails",
    },
    {
        "name": "ScanAndAnalyzeFiles",
        "label": "Scan & Analyze Files",
        "desc": "Scan all files and run media analysis",
    },
    {
        "name": "GenerateIntroVideoMarkers",
        "label": "Detect Intros",
        "desc": "Detect intro sequences across all libraries",
    },
    {
        "name": "GenerateEndCreditsMarkers",
        "label": "Detect Credits",
        "desc": "Detect end-credit sequences (PlexPass)",
    },
    {
        "name": "GenerateMediaIndexFiles",
        "label": "Generate Index Files",
        "desc": "Generate media index files for faster seeking",
    },
    {
        "name": "RecheckPendingIntroVideoMarkers",
        "label": "Recheck Intro Markers",
        "desc": "Re-check pending intro detection tasks",
    },
]


def _plex_client() -> PlexClient | None:
    """Return a PlexClient if Plex is configured, else None."""
    if settings.plex_url and settings.plex_token:
        return PlexClient(settings.plex_url, settings.plex_token)
    return None


def _plex_tv_client() -> PlexTVClient | None:
    """Return a PlexTVClient if Plex token is available, else None."""
    if settings.plex_token:
        return PlexTVClient(settings.plex_token)
    return None


async def _plex_client_for_user(user_id: int) -> PlexClient | None:
    """Return a PlexClient scoped to a home user's token when user_id > 0.

    Calls plex.tv to exchange the admin token for a managed-user token.
    Falls back to the admin token if the switch fails or user_id is 0.
    """
    if not settings.plex_url or not settings.plex_token:
        return None
    if user_id:
        tv = _plex_tv_client()
        if tv:
            try:
                token = await tv.switch_home_user(user_id)
                if token:
                    return PlexClient(settings.plex_url, token)
            except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                pass
            finally:
                await tv.close()
    return PlexClient(settings.plex_url, settings.plex_token)


def _plex_thumb_url(path: str) -> str:
    """Build a proxied Plex thumbnail URL (keeps token server-side)."""
    import urllib.parse

    return f"/web/plex/thumb?path={urllib.parse.quote(path, safe='')}"


def _prowlarr_client():
    """Return ProwlarrClient if configured, else None."""
    if settings.prowlarr_url and settings.prowlarr_api_key:
        from arrmate.clients.prowlarr import ProwlarrClient

        return ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
    return None


_DISCOVER_CATEGORIES = {
    "trending_movies": ("trending movies", "movie"),
    "trending_tv": ("trending TV shows", "tv"),
    "upcoming": ("upcoming movies", "movie"),
    "now_playing": ("movies in theatres", "movie"),
    "on_the_air": ("TV shows on the air", "tv"),
    "popular_movies": ("popular movies", "movie"),
    "popular_tv": ("popular TV shows", "tv"),
    "top_rated_movies": ("top rated movies", "movie"),
    "top_rated_tv": ("top rated TV shows", "tv"),
    # Music (Last.fm)
    "top_artists": ("top artists", "music"),
    "top_tracks": ("top tracks", "music"),
    # Books (Open Library)
    "books_trending": ("trending books", "book"),
    "books_weekly": ("trending this week", "book"),
    "books_fiction": ("fiction", "book"),
    "books_mystery": ("mystery & thriller", "book"),
    "books_scifi": ("science fiction", "book"),
    # Audiobooks (ReadMeABook)
    "audiobooks_popular": ("popular audiobooks", "audiobook"),
    "audiobooks_new": ("new audiobook releases", "audiobook"),
}


_MUSIC_CATEGORIES = {"top_artists", "top_tracks"}


_BOOK_CATEGORIES = {
    "books_trending",
    "books_weekly",
    "books_fiction",
    "books_mystery",
    "books_scifi",
}


_AUDIOBOOK_CATEGORIES = {"audiobooks_popular", "audiobooks_new"}


def _tmdb_client() -> TMDBClient | None:
    if settings.tmdb_api_key:
        return TMDBClient(settings.tmdb_api_key)
    return None


def _lastfm_client() -> LastFMClient | None:
    if settings.lastfm_api_key:
        return LastFMClient(settings.lastfm_api_key)
    return None


def _format_size(size_bytes: int) -> str:
    """Format bytes into a human-readable string."""
    if not size_bytes:
        return ""
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"
