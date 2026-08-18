"""Tests for the agent layer: store, deps, tools."""

import asyncio
from unittest.mock import MagicMock

import pytest
from pydantic_ai.exceptions import RunCancelled
from pydantic_ai.messages import (
    FinalResultEvent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    UserPromptPart,
)

from arrmate.agent import chat
from arrmate.agent.chat import _text_chunk
from arrmate.agent.deps import AgentDeps
from arrmate.agent.tools import _MAX_LIST_ITEMS, _compact, _wrap


class TestCompact:
    def test_strips_nulls_and_empties(self):
        assert _compact({"a": None, "b": "", "c": [], "d": {}, "e": 1}) == {"e": 1}

    def test_keeps_a_whole_library(self):
        """A 40-show library must survive intact; capping it sent the model hunting."""
        assert _compact(list(range(40))) == list(range(40))

    def test_truncates_lists_with_marker(self):
        out = _compact(list(range(_MAX_LIST_ITEMS + 10)))
        assert len(out) == _MAX_LIST_ITEMS + 1
        assert "omitted" in out[-1]
        assert "will not reveal" in out[-1]

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
        big = [ModelRequest(parts=[UserPromptPart(content=f"m{i}")]) for i in range(100)]
        s.save_history(tid, ModelMessagesTypeAdapter.dump_json(big).decode())
        loaded = s.load_history(tid)
        assert len(loaded) == 60
        assert loaded[-1].parts[0].content == "m99"

    def test_corrupt_history_returns_none(self):
        s = self.store
        tid = s.create_thread("u1")
        s.save_history(tid, "{not json")
        assert s.load_history(tid) is None


class TestHistoryRoundTrip:
    """The agent needs ModelMessage objects; handing it raw dicts fails inside pydantic-ai."""

    def test_load_returns_model_messages(self, tmp_chat_db):
        thread_id = tmp_chat_db.create_thread("u")
        saved = ModelMessagesTypeAdapter.dump_json(
            [
                ModelRequest(parts=[UserPromptPart(content="hi")]),
                ModelResponse(parts=[TextPart(content="hello")]),
            ]
        ).decode()
        tmp_chat_db.save_history(thread_id, saved)

        history = tmp_chat_db.load_history(thread_id)

        assert history is not None
        assert [type(m) for m in history] == [ModelRequest, ModelResponse]

    def test_unreadable_history_is_dropped(self, tmp_chat_db):
        thread_id = tmp_chat_db.create_thread("u")
        tmp_chat_db.save_history(thread_id, '[{"not": "a message"}]')

        assert tmp_chat_db.load_history(thread_id) is None

    def test_empty_history_is_none(self, tmp_chat_db):
        thread_id = tmp_chat_db.create_thread("u")
        tmp_chat_db.save_history(thread_id, "[]")

        assert tmp_chat_db.load_history(thread_id) is None


class TestTextChunk:
    """The reply's opening words arrive on part-start, the rest as deltas; both must stream."""

    def test_reads_part_start(self):
        ev = PartStartEvent(index=0, part=TextPart(content="Hello"))
        assert _text_chunk(ev) == "Hello"

    def test_reads_delta(self):
        ev = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" there"))
        assert _text_chunk(ev) == " there"

    def test_ignores_thinking(self):
        ev = PartStartEvent(index=0, part=ThinkingPart(content="pondering"))
        assert _text_chunk(ev) == ""

    def test_ignores_final_result_marker(self):
        """Breaking on this event was what silenced the whole answer."""
        assert _text_chunk(FinalResultEvent(tool_name=None, tool_call_id=None)) == ""


class TestStoredMessageShape:
    """A reloaded thread renders through the same keys the live stream builds."""

    def test_list_messages_uses_text_key(self, tmp_chat_db):
        thread_id = tmp_chat_db.create_thread("u")
        tmp_chat_db.add_message(thread_id, "user", "hello")
        tmp_chat_db.add_message(thread_id, "assistant", "hi back")

        messages = tmp_chat_db.list_messages(thread_id)

        assert [(m["role"], m["text"]) for m in messages] == [
            ("user", "hello"),
            ("assistant", "hi back"),
        ]
        assert all(m["cards"] == [] for m in messages)


class TestHeartbeat:
    """A long tool call sends nothing; without pings the page looks frozen."""

    @pytest.mark.asyncio
    async def test_pings_while_the_agent_is_quiet(self, monkeypatch):
        monkeypatch.setattr(chat, "_HEARTBEAT_SECONDS", 0.01)

        async def slow():
            yield "event: meta\ndata: {}\n\n"
            await asyncio.sleep(0.05)
            yield "event: done\ndata: {}\n\n"

        frames = [f async for f in chat._with_heartbeat(slow())]

        assert frames[0].startswith("event: meta")
        assert frames[-1].startswith("event: done")
        assert any(f.startswith("event: ping") for f in frames)

    @pytest.mark.asyncio
    async def test_passes_events_through_untouched(self):
        async def quick():
            yield "event: delta\ndata: {}\n\n"
            yield "event: done\ndata: {}\n\n"

        assert [f async for f in chat._with_heartbeat(quick())] == [
            "event: delta\ndata: {}\n\n",
            "event: done\ndata: {}\n\n",
        ]

    @pytest.mark.asyncio
    async def test_propagates_errors(self):
        async def boom():
            yield "event: meta\ndata: {}\n\n"
            raise RuntimeError("upstream died")

        with pytest.raises(RuntimeError, match="upstream died"):
            [f async for f in chat._with_heartbeat(boom())]


class TestReleaseProjection:
    """A grab is keyed on guid + indexerId; a search result missing either cannot be pushed."""

    def test_indexer_id_survives_compaction(self):
        release = {
            "guid": "https://indexer.example/abc",
            "indexerId": 12,
            "title": "Futurama S11E01 1080p WEB",
            "seeders": 2557,
            "rejections": [],
        }
        assert _compact(release)["indexerId"] == 12


class TestDeadline:
    """A run that runs long enough to matter still has to end somewhere."""

    def test_not_yet_expired(self):
        assert chat._deadline_expired(started_at=0.0, now=100.0) is False

    def test_expired_at_the_boundary(self):
        from arrmate.agent.models import RUN_DEADLINE_SECONDS

        assert chat._deadline_expired(started_at=0.0, now=RUN_DEADLINE_SECONDS) is True

    def test_expired_well_past(self):
        from arrmate.agent.models import RUN_DEADLINE_SECONDS

        assert chat._deadline_expired(started_at=0.0, now=RUN_DEADLINE_SECONDS + 1) is True


class TestInbox:
    @pytest.fixture(autouse=True)
    def db(self, tmp_chat_db):
        self.store = tmp_chat_db

    def test_queue_then_take_marks_consumed(self):
        tid = self.store.create_thread("u1")
        self.store.queue_message(tid, "first")
        self.store.queue_message(tid, "second")

        assert self.store.peek_queued_count(tid) == 2
        assert self.store.take_queued(tid) == ["first", "second"]
        assert self.store.peek_queued_count(tid) == 0
        assert self.store.take_queued(tid) == []

    def test_take_is_scoped_to_thread(self):
        t1 = self.store.create_thread("u1")
        t2 = self.store.create_thread("u1")
        self.store.queue_message(t1, "for t1")

        assert self.store.take_queued(t2) == []
        assert self.store.take_queued(t1) == ["for t1"]


class TestStopFlag:
    @pytest.fixture(autouse=True)
    def db(self, tmp_chat_db):
        self.store = tmp_chat_db

    def test_set_is_clear(self):
        tid = self.store.create_thread("u1")
        assert self.store.is_stopped(tid) is False

        self.store.set_stop(tid)
        assert self.store.is_stopped(tid) is True

        self.store.clear_stop(tid)
        assert self.store.is_stopped(tid) is False

    def test_set_is_idempotent(self):
        tid = self.store.create_thread("u1")
        self.store.set_stop(tid)
        self.store.set_stop(tid)
        assert self.store.is_stopped(tid) is True


class TestDeliverOrQueue:
    """The queue endpoint routes to a live run when one exists, else falls back to the DB."""

    @pytest.fixture(autouse=True)
    def db(self, tmp_chat_db):
        self.store = tmp_chat_db

    def teardown_method(self):
        chat._active_runs.clear()

    def test_delivers_to_a_live_run(self):
        tid = self.store.create_thread("u1")
        run = MagicMock()
        chat._active_runs[tid] = run

        delivered = chat._deliver_or_queue(tid, "steer this")

        assert delivered is True
        run.enqueue.assert_called_once_with("steer this", priority="asap")
        assert self.store.peek_queued_count(tid) == 0

    def test_falls_back_to_the_db_without_a_live_run(self):
        tid = self.store.create_thread("u1")

        delivered = chat._deliver_or_queue(tid, "no run yet")

        assert delivered is False
        assert self.store.take_queued(tid) == ["no run yet"]


class TestStopRunHelper:
    @pytest.fixture(autouse=True)
    def db(self, tmp_chat_db):
        self.store = tmp_chat_db

    def teardown_method(self):
        chat._active_runs.clear()

    def test_cancels_a_live_run(self):
        tid = self.store.create_thread("u1")
        run = MagicMock()
        chat._active_runs[tid] = run

        live = chat._stop_run(tid)

        assert live is True
        run.cancel.assert_called_once()
        assert self.store.is_stopped(tid) is False

    def test_flags_when_nothing_is_live(self):
        tid = self.store.create_thread("u1")

        live = chat._stop_run(tid)

        assert live is False
        assert self.store.is_stopped(tid) is True


class TestPersistCancelledRun:
    """A stopped run must not lose its transcript."""

    @pytest.fixture(autouse=True)
    def db(self, tmp_chat_db):
        self.store = tmp_chat_db

    def test_persists_the_cancelled_run_history(self):
        tid = self.store.create_thread("u1")
        messages = [
            ModelRequest(parts=[UserPromptPart(content="diagnose the stuck download")]),
            ModelResponse(parts=[TextPart(content="checking the queue now")]),
        ]
        cancelled = RunCancelled("stopped", messages=messages)

        chat._persist_run_outcome(
            tid, "checking the queue now", cancelled.all_messages_json().decode()
        )

        assert [m["text"] for m in self.store.list_messages(tid)] == ["checking the queue now"]
        history = self.store.load_history(tid)
        assert [type(m) for m in history] == [ModelRequest, ModelResponse]
