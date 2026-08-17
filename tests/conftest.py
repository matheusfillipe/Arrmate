"""Shared fixtures: isolated temp databases per test."""

from pathlib import Path

import pytest

import arrmate.agent.store as chat_store
import arrmate.auth.user_db as user_db


@pytest.fixture
def tmp_user_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(user_db, "_db_path", lambda: tmp_path / "users.db")
    monkeypatch.setattr(user_db, "_auth_json_path", lambda: tmp_path / "auth.json")
    user_db._db_ready = False
    user_db.init_db()
    return user_db


@pytest.fixture
def tmp_chat_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(chat_store, "_db_path", lambda: tmp_path / "chat.db")
    chat_store.init_db()
    return chat_store
