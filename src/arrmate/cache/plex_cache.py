"""SQLite cache for Plex watch history.

Stores a local copy of Plex watch history so the By Title view loads instantly
instead of fetching from the Plex API on every page load.
Cache is refreshed on demand (manual sync) or when first loading after startup.
"""

import logging
import sqlite3
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import httpx
from pydantic import BaseModel

from arrmate.clients.plex import PlexMetadata

logger = logging.getLogger(__name__)


class CachedHistoryEntry(BaseModel):
    rating_key: str | None = None
    title: str
    grandparent_title: str | None = None
    type: str
    thumb: str | None = None
    grandparent_thumb: str | None = None
    viewed_at: int
    account_id: int | None = None


# Time-to-live in seconds before the cache is considered stale
CACHE_TTL = 15 * 60  # 15 minutes


def _db_path() -> Path:
    from arrmate.config.settings import settings

    return Path(settings.auth_data_dir) / "plex_cache.db"


@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_cache() -> None:
    """Create tables if they don't exist."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS plex_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rating_key TEXT,
                title TEXT NOT NULL,
                grandparent_title TEXT,
                type TEXT NOT NULL,
                thumb TEXT,
                grandparent_thumb TEXT,
                viewed_at INTEGER NOT NULL,
                account_id INTEGER,
                synced_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plex_cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.commit()


def _ensure_init() -> None:
    try:
        init_cache()
    except sqlite3.Error as e:
        logger.warning("Could not init Plex cache: %s", e)


def get_last_synced() -> int | None:
    """Return Unix timestamp of last successful sync, or None."""
    try:
        _ensure_init()
        with _get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM plex_cache_meta WHERE key='last_synced'"
            ).fetchone()
            return int(row["value"]) if row else None
    except (httpx.HTTPError, ValueError):
        return None


def is_stale() -> bool:
    """Return True if cache is empty or older than CACHE_TTL."""
    last = get_last_synced()
    if last is None:
        return True
    return (time.time() - last) > CACHE_TTL


def get_cache_size() -> int:
    """Return number of rows in the history cache."""
    try:
        _ensure_init()
        with _get_conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM plex_history").fetchone()[0])
    except (httpx.HTTPError, ValueError):
        return 0


def populate_cache(items: list[PlexMetadata]) -> int:
    """Replace cache with a fresh list of Plex history items.

    Args:
        items: Raw Plex history Metadata items from the API.

    Returns:
        Number of rows stored.
    """
    now = int(time.time())
    try:
        _ensure_init()
        with _get_conn() as conn:
            conn.execute("DELETE FROM plex_history")
            conn.executemany(
                """INSERT INTO plex_history
                   (rating_key, title, grandparent_title, type, thumb, grandparent_thumb,
                    viewed_at, account_id, synced_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        item.rating_key,
                        item.title or "",
                        item.grandparent_title,
                        item.type or "",
                        item.thumb,
                        item.grandparent_thumb,
                        item.viewed_at or 0,
                        item.account_id,
                        now,
                    )
                    for item in items
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO plex_cache_meta (key, value) VALUES ('last_synced', ?)",
                (str(now),),
            )
            conn.commit()
            return len(items)
    except (httpx.HTTPError, sqlite3.Error, ValueError) as e:
        logger.error("Failed to populate Plex cache: %s", e)
        return 0


def get_cached_history() -> list[CachedHistoryEntry]:
    """Return all cached history rows, newest first."""
    try:
        _ensure_init()
        with _get_conn() as conn:
            rows = conn.execute("SELECT * FROM plex_history ORDER BY viewed_at DESC").fetchall()
            return [CachedHistoryEntry.model_validate(dict(row)) for row in rows]
    except sqlite3.Error as e:
        logger.warning("Failed to read Plex cache: %s", e)
        return []
