"""Chat routes: pages, thread management, and the SSE agent stream."""

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncIterator
from typing import Literal, TypedDict

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, JsonValue, ValidationError
from pydantic_ai import Agent, AgentRun, FunctionToolCallEvent, FunctionToolResultEvent
from pydantic_ai.exceptions import RunCancelled, UsageLimitExceeded
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from arrmate.auth import user_db
from arrmate.auth.dependencies import get_current_user
from arrmate.auth.models import SessionUser
from arrmate.config.settings import settings
from arrmate.interfaces.web.routes import templates

from . import store
from .compaction import compact, settle_tool_calls
from .deps import AgentDeps
from .models import MAX_TOOL_CALLS_PER_RUN, RUN_DEADLINE_SECONDS, RUN_USAGE_LIMITS, get_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web/chat", tags=["chat"])

_TOOL_RESULT_PREVIEW = 400
_HEARTBEAT_SECONDS = 5.0

#: Runs currently streaming, keyed by thread_id, so /queue and /stop can reach a live run
#: directly instead of only being able to leave it a note for its next turn.
_active_runs: dict[str, AgentRun[AgentDeps, str]] = {}


class RunSession:
    """A run in flight, decoupled from whoever is watching it.

    Frames are appended to a log rather than written straight to a socket, so a reader can
    join late and replay everything it missed, and a reader leaving costs the run nothing.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self.frames: list[str] = []
        self.finished = False
        self.updated = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def emit(self, frame: str) -> None:
        self.frames.append(frame)
        self.updated.set()

    def close(self) -> None:
        self.finished = True
        self.updated.set()

    async def follow(self, start: int = 0) -> AsyncIterator[str]:
        """Yield every frame from `start` onwards, waiting for more until the run ends."""
        cursor = start
        while True:
            while cursor < len(self.frames):
                yield self.frames[cursor]
                cursor += 1
            if self.finished:
                return
            self.updated.clear()
            await self.updated.wait()


#: Runs in flight, keyed by thread_id. Outlives any particular HTTP request.
_sessions: dict[str, RunSession] = {}


def _deadline_expired(started_at: float, now: float) -> bool:
    return now - started_at >= RUN_DEADLINE_SECONDS


def _context_tokens(run: AgentRun[AgentDeps, str]) -> int:
    """Tokens sitting in the model's context on the most recent request.

    `usage.input_tokens` accumulates over every request in the run, so the latest request's
    input — the part that has to fit in the window — is the difference between requests.
    """
    usage = run.usage
    if not usage or not usage.requests:
        return 0
    return int(usage.input_tokens // usage.requests)


def _deliver_or_queue(thread_id: str, text: str) -> bool:
    """Get a steering message to a thread's run. Returns whether it reached a live run.

    `AgentRun.enqueue` drains between tool execution and the next model request — never
    mid-tool — so a message dropped in here lands at exactly the boundary a human would want.
    Without a live run there is nothing to enqueue into, so it waits in run_inbox for the next
    turn to start.
    """
    run = _active_runs.get(thread_id)
    if run is not None:
        run.enqueue(text, priority="asap")
        return True
    store.queue_message(thread_id, text)
    return False


def _stop_run(thread_id: str) -> bool:
    """Stop a thread's run. Returns whether a live run was cancelled directly."""
    # The flag is set either way: cancelling a live run raises RunCancelled without going back
    # through the node loop, so this is what lets the stream tell "the user stopped it" apart
    # from an internal cancellation.
    store.set_stop(thread_id)
    run = _active_runs.get(thread_id)
    if run is not None:
        run.cancel()
        return True
    return False


def _checkpoint_history(thread_id: str, run: AgentRun[AgentDeps, str]) -> None:
    """Save the run's messages so far, between batches of tool calls.

    History was only written when a run finished, so a restart in the middle of a long one
    threw away every tool call it had made and left the next turn reading the previous run's
    messages: the agent answered about whatever it had been doing before. Between tool
    batches the message list is complete (no call is left without its return), which makes it
    the one point that is always safe to save. A failure here must not take the run with it.
    """
    try:
        store.save_history(thread_id, run.all_messages_json().decode())
    except sqlite3.Error as e:
        logger.warning("could not checkpoint history on thread %s: %s", thread_id, e)


def _persist_run_outcome(thread_id: str, final_text: str, history_json: str) -> None:
    # A run stopped before it said anything leaves no assistant turn to show, but its history
    # still matters: the next turn needs to see the tools it already ran.
    if final_text:
        store.add_message(thread_id, "assistant", final_text)
    store.save_history(thread_id, history_json)


def _persist_failed_run(thread_id: str, streamed_text: str, failure: str) -> None:
    """Keep what a failed run already said, and why it stopped, as its assistant turn.

    The history is checkpointed between tool batches, but the rendered turn is written only
    when a run ends cleanly, so without this a reload shows the user's messages and nothing
    the agent said in between.
    """
    try:
        store.add_message(
            thread_id, "assistant", f"{streamed_text}\n\n**Run failed:** {failure}".strip()
        )
    except sqlite3.Error as e:
        logger.warning("could not save the failed turn on thread %s: %s", thread_id, e)


class _ChatPageContext(TypedDict):
    current_user: SessionUser
    unread_count: int
    threads: list[store.ThreadSummary]
    thread: store.Thread | None
    messages: list[store.ChatTurn]


class _ChatBody(BaseModel):
    thread_id: str | None = None
    message: str | None = None


class _ThreadCreated(BaseModel):
    thread_id: str


class _ThreadDeleted(BaseModel):
    deleted: bool


class _QueueResult(BaseModel):
    delivered: bool
    pending: int


class _StopResult(BaseModel):
    stopped: bool
    live: bool


class _LiveStatus(BaseModel):
    live: bool


class _MetaFrame(BaseModel):
    thread_id: str


class _MessageFrame(BaseModel):
    message: str


class _DeltaFrame(BaseModel):
    text: str


class _DeliveredFrame(BaseModel):
    count: int


class _ToolStartFrame(BaseModel):
    id: str
    name: str
    phase: Literal["start"] = "start"
    args: str | dict[str, JsonValue] | None = None


class _ToolEndFrame(BaseModel):
    id: str
    name: str | None = None
    phase: Literal["end"] = "end"
    result: str


class _ProgressFrame(BaseModel):
    tool_calls: int
    elapsed_seconds: int
    context_tokens: int
    context_window: int


SseEvent = Literal["meta", "notice", "delta", "delivered", "tool", "progress", "error"]


def _sse(event: SseEvent, payload: BaseModel) -> str:
    return f"event: {event}\ndata: {payload.model_dump_json()}\n\n"


def _render_page(
    request: Request,
    user: SessionUser,
    thread: store.Thread | None,
    messages: list[store.ChatTurn],
) -> Response:
    """Render the chat page; the navbar needs the user and unread count like every page."""
    context = _ChatPageContext(
        current_user=user,
        unread_count=user_db.get_unread_count(user.user_id),
        threads=store.list_threads(user.user_id),
        thread=thread,
        messages=messages,
    )
    return templates.TemplateResponse(request, "pages/chat.html", dict(context))


async def _read_body(request: Request) -> _ChatBody:
    try:
        return _ChatBody.model_validate_json(await request.body())
    except ValidationError as e:
        raise HTTPException(status_code=400, detail="invalid JSON body") from e


def _init_once() -> None:
    store.init_db()


async def _with_heartbeat(events: AsyncIterator[str]) -> AsyncIterator[str]:
    """Forward events, emitting a ping whenever the agent goes quiet.

    A single indexer search can run for a minute with nothing to say. Without a byte on the
    wire the page looks frozen and any idle proxy timeout is free to drop the response, so
    silence is filled with pings the client counts as progress.

    The agent run is drained by one task from end to end. Stepping the generator from a fresh
    task per item instead would move the exit of pydantic-ai's cancel scope off the task that
    entered it, which fails the run right after it has produced its answer.
    """
    frames: asyncio.Queue[str | None] = asyncio.Queue()

    async def drain() -> None:
        try:
            async for frame in events:
                await frames.put(frame)
        finally:
            await frames.put(None)

    producer = asyncio.create_task(drain())
    try:
        while True:
            try:
                frame = await asyncio.wait_for(frames.get(), _HEARTBEAT_SECONDS)
            except TimeoutError:
                yield "event: ping\ndata: {}\n\n"
                continue
            if frame is None:
                break
            yield frame
        # The producer is already finished; awaiting it surfaces whatever it raised.
        await producer
    finally:
        producer.cancel()


def _text_chunk(event: AgentStreamEvent) -> str:
    """Answer text carried by a model-stream event, if any.

    The opening piece of a reply arrives on the part-start event and the rest as deltas, so
    both shapes have to be read or the first words go missing. Thinking parts are skipped;
    only the answer reaches the user.
    """
    match event:
        case PartStartEvent(part=TextPart(content=text)):
            return text
        case PartDeltaEvent(delta=TextPartDelta(content_delta=text)):
            return text
        case _:
            return ""


def _require_user(request: Request) -> SessionUser:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="authentication required")
    return user


def _require_thread(thread_id: str | None, user: SessionUser) -> str:
    if not thread_id or not store.get_thread(thread_id, user.user_id):
        raise HTTPException(status_code=404, detail="thread not found")
    return thread_id


def _required_message(body: _ChatBody) -> str:
    text = (body.message or "").strip()[:2000]
    if not text:
        raise HTTPException(status_code=422, detail="message is required")
    return text


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request) -> Response:
    user = _require_user(request)
    _init_once()
    return _render_page(request, user, None, [])


@router.get("/{thread_id}", response_class=HTMLResponse)
async def chat_thread_page(request: Request, thread_id: str) -> Response:
    user = _require_user(request)
    _init_once()
    thread = store.get_thread(thread_id, user.user_id)
    if not thread:
        raise HTTPException(status_code=404, detail="thread not found")
    return _render_page(request, user, thread, store.list_messages(thread_id))


@router.post("/thread")
async def create_thread(request: Request) -> _ThreadCreated:
    user = _require_user(request)
    return _ThreadCreated(thread_id=store.create_thread(user.user_id))


@router.post("/thread/{thread_id}/delete")
async def delete_thread(request: Request, thread_id: str) -> _ThreadDeleted:
    user = _require_user(request)
    return _ThreadDeleted(deleted=store.delete_thread(thread_id, user.user_id))


@router.post("/queue")
async def queue_message(request: Request) -> _QueueResult:
    """Steer a running thread, or leave a note for its next turn if nothing is running."""
    user = _require_user(request)
    _init_once()
    body = await _read_body(request)
    text = _required_message(body)
    thread_id = _require_thread(body.thread_id, user)

    store.add_message(thread_id, "user", text)
    delivered = _deliver_or_queue(thread_id, text)
    return _QueueResult(
        delivered=delivered,
        pending=0 if delivered else store.peek_queued_count(thread_id),
    )


@router.post("/stop")
async def stop_run(request: Request) -> _StopResult:
    user = _require_user(request)
    _init_once()
    thread_id = _require_thread((await _read_body(request)).thread_id, user)
    return _StopResult(stopped=True, live=_stop_run(thread_id))


@router.post("/attach", response_model=None)
async def attach_run(request: Request) -> StreamingResponse:
    """Re-join a run that is still going, replaying everything emitted so far."""
    user = _require_user(request)
    thread_id = _require_thread((await _read_body(request)).thread_id, user)

    session = _sessions.get(thread_id)
    if session is None:
        raise HTTPException(status_code=409, detail="no run in flight")

    return StreamingResponse(
        _with_heartbeat(session.follow()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{thread_id}/live")
async def run_is_live(request: Request, thread_id: str) -> _LiveStatus:
    _require_user(request)
    session = _sessions.get(thread_id)
    return _LiveStatus(live=session is not None and not session.finished)


@router.post("/stream", response_model=None)
async def chat_stream(request: Request) -> StreamingResponse:
    user = _require_user(request)
    _init_once()
    body = await _read_body(request)
    message = _required_message(body)
    thread_id = body.thread_id
    if not thread_id or not store.get_thread(thread_id, user.user_id):
        thread_id = store.create_thread(user.user_id)

    store.add_message(thread_id, "user", message)
    store.auto_title(thread_id, message)

    deps = AgentDeps(
        user_id=user.user_id,
        username=user.username,
        role=user.role,
        thread_id=thread_id,
    )

    async def event_stream() -> AsyncIterator[str]:
        yield _sse("meta", _MetaFrame(thread_id=thread_id))
        streamed = False
        started_at = time.monotonic()
        tool_calls = 0
        compacted_total = 0
        accumulated_text = ""
        cancel_notice = ""
        store.clear_stop(thread_id)

        # A message queued while nothing was running for this thread (see /queue's fallback
        # path) rides in as extra input on the next turn instead of being lost.
        queued = store.take_queued(thread_id)
        prompt = "\n\n".join([*queued, message]) if queued else message

        try:
            agent: Agent[AgentDeps, str] = get_agent()
            history = store.load_history(thread_id)
            if history:
                # A run stopped mid-tool leaves its last call unanswered, which makes
                # every later prompt on this thread fail outright. Repair on load so
                # threads already saved in that state come back to life too.
                settled = settle_tool_calls(history)
                if settled:
                    store.save_history(
                        thread_id, ModelMessagesTypeAdapter.dump_json(history).decode()
                    )
                    yield _sse(
                        "notice",
                        _MessageFrame(
                            message="The previous run was stopped mid-tool; picking up "
                            "from where it left off."
                        ),
                    )
                history, stripped = compact(history, settings.context_window_tokens)
                if stripped:
                    yield _sse(
                        "notice",
                        _MessageFrame(
                            message=f"Context was filling up; cleared {stripped} older tool "
                            "results to make room."
                        ),
                    )
            run_cancelled: RunCancelled | None = None

            try:
                async with agent.iter(
                    prompt,
                    deps=deps,
                    message_history=history,
                    usage_limits=RUN_USAGE_LIMITS,
                ) as run:
                    _active_runs[thread_id] = run
                    pending_seen = 0
                    try:
                        async for node in run:
                            # pending_messages drains when the library hands a queued message
                            # to the model. Telling the client the moment that happens is the
                            # difference between "queued" and "it has read it".
                            # A single long task can fill the window on its own, so the
                            # history the run is carrying gets compacted in place as it goes,
                            # not only between turns.
                            live_history = getattr(run.ctx.state, "message_history", None)
                            if live_history:
                                _, freed = compact(live_history, settings.context_window_tokens)
                                if freed:
                                    compacted_total += freed
                                    yield _sse(
                                        "notice",
                                        _MessageFrame(
                                            message="Context was nearly full; cleared "
                                            f"{freed} older tool results to keep going."
                                        ),
                                    )

                            depth = len(run.pending_messages)
                            if depth < pending_seen:
                                yield _sse("delivered", _DeliveredFrame(count=pending_seen - depth))
                            pending_seen = depth
                            if store.is_stopped(thread_id):
                                store.clear_stop(thread_id)
                                cancel_notice = "Stopped by the user."
                                run.cancel()
                                continue
                            if _deadline_expired(started_at, time.monotonic()):
                                cancel_notice = (
                                    f"Hit the {RUN_DEADLINE_SECONDS // 3600}-hour run limit; "
                                    "stopping here."
                                )
                                run.cancel()
                                continue
                            if Agent.is_model_request_node(node):
                                async with node.stream(run.ctx) as stream:
                                    async for ev in stream:
                                        chunk = _text_chunk(ev)
                                        if chunk:
                                            streamed = True
                                            accumulated_text += chunk
                                            yield _sse("delta", _DeltaFrame(text=chunk))
                            elif Agent.is_call_tools_node(node):
                                async with node.stream(run.ctx) as stream:
                                    async for ev in stream:
                                        if isinstance(ev, FunctionToolCallEvent):
                                            yield _sse(
                                                "tool",
                                                _ToolStartFrame(
                                                    id=ev.part.tool_call_id,
                                                    name=ev.part.tool_name,
                                                    args=ev.part.args,
                                                ),
                                            )
                                        elif isinstance(ev, FunctionToolResultEvent):
                                            tool_calls += 1
                                            yield _sse(
                                                "tool",
                                                _ToolEndFrame(
                                                    id=ev.part.tool_call_id,
                                                    name=ev.part.tool_name,
                                                    # The payload rides on the part; the event's
                                                    # own `content` is unset for tool returns.
                                                    result=str(ev.part.content)[
                                                        :_TOOL_RESULT_PREVIEW
                                                    ],
                                                ),
                                            )
                                            yield _sse(
                                                "progress",
                                                _ProgressFrame(
                                                    tool_calls=tool_calls,
                                                    elapsed_seconds=int(
                                                        time.monotonic() - started_at
                                                    ),
                                                    context_tokens=_context_tokens(run),
                                                    context_window=settings.context_window_tokens,
                                                ),
                                            )
                                _checkpoint_history(thread_id, run)
                    finally:
                        _active_runs.pop(thread_id, None)
            except RunCancelled as e:
                run_cancelled = e

            if run_cancelled is not None:
                if not cancel_notice and store.is_stopped(thread_id):
                    cancel_notice = "Stopped by the user."
                store.clear_stop(thread_id)
                final_text = accumulated_text
                history_json = run_cancelled.all_messages_json().decode()
                yield _sse("notice", _MessageFrame(message=cancel_notice or "Run was cancelled."))
            else:
                result = run.result
                final_text = result.output if result else ""
                history_json = result.all_messages_json().decode() if result else "[]"

            if final_text and not streamed:
                yield _sse("delta", _DeltaFrame(text=final_text))
            _persist_run_outcome(thread_id, final_text, history_json)
            yield "event: done\ndata: {}\n\n"
        except UsageLimitExceeded:
            logger.warning("chat run hit the tool-call ceiling on thread %s", thread_id)
            failure = (
                f"Gave up after {MAX_TOOL_CALLS_PER_RUN} tool calls without "
                "reaching an answer. Try asking something narrower."
            )
            _persist_failed_run(thread_id, accumulated_text, failure)
            yield _sse("error", _MessageFrame(message=failure))
        except Exception as e:
            logger.exception("chat stream failed for thread %s", thread_id)
            failure = f"{type(e).__name__}: {e}"[:300]
            _persist_failed_run(thread_id, accumulated_text, failure)
            yield _sse("error", _MessageFrame(message=failure))

    session = RunSession(thread_id)
    _sessions[thread_id] = session

    async def pump() -> None:
        try:
            async for frame in event_stream():
                session.emit(frame)
        finally:
            session.close()
            _sessions.pop(thread_id, None)

    # The run owns its own task. Nothing about it is tied to this response, so the tab can go
    # away mid-task and the work still finishes and persists.
    session.task = asyncio.create_task(pump())

    return StreamingResponse(
        _with_heartbeat(session.follow()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
