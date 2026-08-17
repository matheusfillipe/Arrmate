"""SQLite persistence for chat threads and pydantic-ai message history."""

import json
import logging
import secrets
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_THREAD_HISTORY_MAX = 60


def _db_path() -> Path:
    from ..config.settings import settings

    return Path(settings.auth_data_dir) / "chat.db"


@contextmanager
def _get_conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def init_db() -> None:
    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS threads (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New chat',
                model TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_messages_thread
                ON messages (thread_id, id);
            CREATE TABLE IF NOT EXISTS thread_history (
                thread_id TEXT PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
                history TEXT NOT NULL
            );
            """)
        conn.commit()


def create_thread(user_id: str, title: str = "New chat", model: str = "") -> str:
    thread_id = secrets.token_hex(6)
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO threads (id, user_id, title, model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (thread_id, user_id, title, model, _now(), _now()),
        )
        conn.commit()
    return thread_id


def list_threads(user_id: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, model, updated_at FROM threads WHERE user_id = ? "
            "ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_thread(thread_id: str, user_id: str) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM threads WHERE id = ? AND user_id = ?",
            (thread_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def delete_thread(thread_id: str, user_id: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM threads WHERE id = ? AND user_id = ?",
            (thread_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def rename_thread(thread_id: str, user_id: str, title: str) -> bool:
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE threads SET title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (title, _now(), thread_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def add_message(thread_id: str, role: str, content: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO messages (thread_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, role, content, _now()),
        )
        conn.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (_now(), thread_id))
        conn.commit()


def list_messages(thread_id: str) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages WHERE thread_id = ? ORDER BY id",
            (thread_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def auto_title(thread_id: str, first_message: str) -> bool:
    """Set the thread title from its first user message if still default."""
    title = first_message.strip().splitlines()[0][:60] or "New chat"
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE threads SET title = ? WHERE id = ? AND title = 'New chat'",
            (title, thread_id),
        )
        conn.commit()
        return cur.rowcount > 0


def save_history(thread_id: str, history_json: str) -> None:
    """Persist the serialized pydantic-ai message history for a thread."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO thread_history (thread_id, history) VALUES (?, ?)",
            (thread_id, history_json),
        )
        conn.commit()


def load_history(thread_id: str) -> list | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT history FROM thread_history WHERE thread_id = ?", (thread_id,)
        ).fetchone()
    if not row or not row["history"]:
        return None
    try:
        data = json.loads(row["history"])
    except json.JSONDecodeError:
        logger.warning("thread %s history corrupted, dropping", thread_id)
        return None
    return data[-_THREAD_HISTORY_MAX:] if isinstance(data, list) else None
