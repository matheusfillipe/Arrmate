"""Tests for the agent layer: store, deps, tools."""

import pytest

from arrmate.agent.deps import AgentDeps
from arrmate.agent.tools import _compact, _wrap


class TestCompact:
    def test_strips_nulls_and_empties(self):
        assert _compact({"a": None, "b": "", "c": [], "d": {}, "e": 1}) == {"e": 1}

    def test_truncates_lists_with_marker(self):
        out = _compact(list(range(40)))
        assert len(out) == 31
        assert "more" in out[-1]

    def test_truncates_long_strings(self):
        out = _compact("x" * 500)
        assert len(out) < 500
        assert out.endswith("…")

    def test_leaves_scalars(self):
        assert _compact(5) == 5
        assert _compact(True) is True

    def test_wrap_uses_data_markers(self):
        wrapped = _wrap({"k": "v"})
        assert wrapped.startswith("<<<TOOL_DATA")
        assert wrapped.endswith("TOOL_DATA>>>")


class TestDeps:
    def test_user_cannot_write(self):
        deps = AgentDeps(user_id="u", username="u", role="user")
        assert deps.can_write is False
        with pytest.raises(PermissionError):
            deps.require_write("add_media")

    def test_power_user_can_write(self):
        deps = AgentDeps(user_id="u", username="u", role="power_user")
        assert deps.can_write is True
        deps.require_write("add_media")

    def test_admin_can_write(self):
        deps = AgentDeps(user_id="u", username="u", role="admin")
        assert deps.can_write is True
        deps.require_write("remove_media")


class TestStore:
    @pytest.fixture(autouse=True)
    def db(self, tmp_chat_db):
        self.store = tmp_chat_db

    def test_thread_lifecycle(self):
        s = self.store
        tid = s.create_thread("u1")
        assert s.get_thread(tid, "u1")["title"] == "New chat"
        assert s.get_thread(tid, "other") is None

        s.add_message(tid, "user", "fix x-men s02e07")
        s.auto_title(tid, "fix x-men s02e07\nsecond line")
        assert s.get_thread(tid, "u1")["title"] == "fix x-men s02e07"

        msgs = s.list_messages(tid)
        assert msgs[0]["role"] == "user"

        assert s.delete_thread(tid, "u1") is True
        assert s.list_threads("u1") == []

    def test_history_roundtrip_and_cap(self):
        s = self.store
        tid = s.create_thread("u1")
        big = [{"role": f"m{i}"} for i in range(100)]
        s.save_history(tid, __import__("json").dumps(big))
        loaded = s.load_history(tid)
        assert len(loaded) == 60
        assert loaded[-1] == {"role": "m99"}

    def test_corrupt_history_returns_none(self):
        s = self.store
        tid = s.create_thread("u1")
        s.save_history(tid, "{not json")
        assert s.load_history(tid) is None
