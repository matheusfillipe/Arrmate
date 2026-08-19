"""Chat routes: pages, thread management, and the SSE agent stream."""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic_ai import Agent, AgentRun, FunctionToolCallEvent, FunctionToolResultEvent
from pydantic_ai.exceptions import RunCancelled, UsageLimitExceeded
from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)

from arrmate.auth import user_db
from arrmate.auth.dependencies import get_current_user
from arrmate.config.settings import settings
from arrmate.interfaces.web.routes import templates

from . import store
from .deps import AgentDeps
from .models import MAX_TOOL_CALLS_PER_RUN, RUN_DEADLINE_SECONDS, RUN_USAGE_LIMITS, get_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web/chat", tags=["chat"])

_TOOL_RESULT_PREVIEW = 400
_HEARTBEAT_SECONDS = 5.0

#: Runs currently streaming, keyed by thread_id, so /queue and /stop can reach a live run
#: directly instead of only being able to leave it a note for its next turn.
_active_runs: dict[str, AgentRun[AgentDeps, str]] = {}


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


def _persist_run_outcome(thread_id: str, final_text: str, history_json: str) -> None:
    # A run stopped before it said anything leaves no assistant turn to show, but its history
    # still matters: the next turn needs to see the tools it already ran.
    if final_text:
        store.add_message(thread_id, "assistant", final_text)
    store.save_history(thread_id, history_json)


def _page_context(user: dict, threads: list, thread: dict | None, messages: list) -> dict:
    """Context shared by both chat page renders (navbar requires both keys)."""
    unread = user_db.get_unread_count(user["user_id"])
    return {
        "current_user": user,
        "unread_count": unread,
        "threads": threads,
        "thread": thread,
        "messages": messages,
    }


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


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request) -> Response:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    _init_once()
    threads = store.list_threads(user["user_id"])
    return templates.TemplateResponse(
        request,
        "pages/chat.html",
        _page_context(user, threads, None, []),
    )


@router.get("/{thread_id}", response_class=HTMLResponse)
async def chat_thread_page(request: Request, thread_id: str) -> Response:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    _init_once()
    thread = store.get_thread(thread_id, user["user_id"])
    if not thread:
        return JSONResponse(status_code=404, content={"detail": "thread not found"})
    return templates.TemplateResponse(
        request,
        "pages/chat.html",
        _page_context(
            user,
            store.list_threads(user["user_id"]),
            thread,
            store.list_messages(thread_id),
        ),
    )


@router.post("/thread", response_model=None)
async def create_thread(request: Request) -> dict | JSONResponse:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    thread_id = store.create_thread(user["user_id"])
    return {"thread_id": thread_id}


@router.post("/thread/{thread_id}/delete", response_model=None)
async def delete_thread(request: Request, thread_id: str) -> dict | JSONResponse:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    ok = store.delete_thread(thread_id, user["user_id"])
    return {"deleted": ok}


@router.post("/queue", response_model=None)
async def queue_message(request: Request) -> dict | JSONResponse:
    """Steer a running thread, or leave a note for its next turn if nothing is running."""
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    _init_once()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})

    thread_id = str(body.get("thread_id") or "")
    text = str(body.get("message") or "").strip()[:2000]
    if not text:
        return JSONResponse(status_code=422, content={"detail": "message is required"})
    if not thread_id or not store.get_thread(thread_id, user["user_id"]):
        return JSONResponse(status_code=404, content={"detail": "thread not found"})

    store.add_message(thread_id, "user", text)
    delivered = _deliver_or_queue(thread_id, text)
    return {
        "delivered": delivered,
        "pending": 0 if delivered else store.peek_queued_count(thread_id),
    }


@router.post("/stop", response_model=None)
async def stop_run(request: Request) -> dict | JSONResponse:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    _init_once()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})

    thread_id = str(body.get("thread_id") or "")
    if not thread_id or not store.get_thread(thread_id, user["user_id"]):
        return JSONResponse(status_code=404, content={"detail": "thread not found"})

    live = _stop_run(thread_id)
    return {"stopped": True, "live": live}


@router.post("/stream", response_model=None)
async def chat_stream(request: Request) -> StreamingResponse | JSONResponse:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    _init_once()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(status_code=400, content={"detail": "invalid JSON body"})

    thread_id = str(body.get("thread_id") or "")
    message = str(body.get("message") or "").strip()[:2000]

    if not message:
        return JSONResponse(status_code=422, content={"detail": "message is required"})
    if not thread_id or not store.get_thread(thread_id, user["user_id"]):
        thread_id = store.create_thread(user["user_id"])

    store.add_message(thread_id, "user", message)
    store.auto_title(thread_id, message)

    deps = AgentDeps(
        user_id=user["user_id"],
        username=user.get("username", ""),
        role=user.get("role", "user"),
        thread_id=thread_id,
    )

    async def event_stream() -> AsyncIterator[str]:
        yield f"event: meta\ndata: {json.dumps({'thread_id': thread_id})}\n\n"
        streamed = False
        started_at = time.monotonic()
        tool_calls = 0
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
                            depth = len(run.pending_messages)
                            if depth < pending_seen:
                                yield (
                                    "event: delivered\ndata: "
                                    + json.dumps({"count": pending_seen - depth})
                                    + "\n\n"
                                )
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
                                            yield (
                                                "event: delta\ndata: "
                                                + json.dumps({"text": chunk})
                                                + "\n\n"
                                            )
                            elif Agent.is_call_tools_node(node):
                                async with node.stream(run.ctx) as stream:
                                    async for ev in stream:
                                        if isinstance(ev, FunctionToolCallEvent):
                                            yield (
                                                "event: tool\ndata: "
                                                + json.dumps(
                                                    {
                                                        "name": ev.part.tool_name,
                                                        "phase": "start",
                                                        "args": ev.part.args,
                                                    }
                                                )
                                                + "\n\n"
                                            )
                                        elif isinstance(ev, FunctionToolResultEvent):
                                            tool_calls += 1
                                            yield (
                                                "event: tool\ndata: "
                                                + json.dumps(
                                                    {
                                                        "name": ev.part.tool_name,
                                                        "phase": "end",
                                                        # The payload rides on the part; the
                                                        # event's own `content` is unset for
                                                        # tool returns.
                                                        "result": str(ev.part.content)[
                                                            :_TOOL_RESULT_PREVIEW
                                                        ],
                                                    }
                                                )
                                                + "\n\n"
                                            )
                                            yield (
                                                "event: progress\ndata: "
                                                + json.dumps(
                                                    {
                                                        "tool_calls": tool_calls,
                                                        "elapsed_seconds": int(
                                                            time.monotonic() - started_at
                                                        ),
                                                        "context_tokens": _context_tokens(run),
                                                        "context_window": (
                                                            settings.context_window_tokens
                                                        ),
                                                    }
                                                )
                                                + "\n\n"
                                            )
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
                yield (
                    "event: notice\ndata: "
                    + json.dumps({"message": cancel_notice or "Run was cancelled."})
                    + "\n\n"
                )
            else:
                result = run.result
                final_text = result.output if result else ""
                history_json = result.all_messages_json().decode() if result else "[]"

            if final_text and not streamed:
                yield "event: delta\ndata: " + json.dumps({"text": final_text}) + "\n\n"
            _persist_run_outcome(thread_id, final_text, history_json)
            yield "event: done\ndata: {}\n\n"
        except UsageLimitExceeded:
            logger.warning("chat run hit the tool-call ceiling on thread %s", thread_id)
            yield (
                "event: error\ndata: "
                + json.dumps(
                    {
                        "message": (
                            f"Gave up after {MAX_TOOL_CALLS_PER_RUN} tool calls without "
                            "reaching an answer. Try asking something narrower."
                        )
                    }
                )
                + "\n\n"
            )
        except Exception as e:
            logger.exception("chat stream failed for thread %s", thread_id)
            yield (
                "event: error\ndata: "
                + json.dumps({"message": f"{type(e).__name__}: {e}"[:300]})
                + "\n\n"
            )

    return StreamingResponse(
        _with_heartbeat(event_stream()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
