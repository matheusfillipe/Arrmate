"""Chat routes: pages, thread management, and the SSE agent stream."""

import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic_ai import Agent, FinalResultEvent, FunctionToolCallEvent, FunctionToolResultEvent
from pydantic_ai.messages import PartDeltaEvent, TextPartDelta

from ..auth import user_db
from ..auth.dependencies import get_current_user
from ..interfaces.web.routes import templates
from . import store
from .deps import AgentDeps
from .models import RUN_USAGE_LIMITS, get_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web/chat", tags=["chat"])

_TOOL_RESULT_PREVIEW = 400


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


@router.post("/stream", response_model=None)
async def chat_stream(request: Request) -> StreamingResponse | JSONResponse:
    user = get_current_user(request)
    if not user:
        return JSONResponse(status_code=401, content={"detail": "authentication required"})
    _init_once()

    try:
        body = await request.json()
    except Exception:
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
        try:
            agent: Agent[AgentDeps, str] = get_agent()
            history = store.load_history(thread_id)

            async with agent.iter(
                message,
                deps=deps,
                message_history=history,
                usage_limits=RUN_USAGE_LIMITS,
            ) as run:
                async for node in run:
                    if Agent.is_model_request_node(node):
                        async with node.stream(run.ctx) as stream:
                            async for ev in stream:
                                if isinstance(ev, FinalResultEvent):
                                    break
                                if (
                                    isinstance(ev, PartDeltaEvent)
                                    and isinstance(ev.delta, TextPartDelta)
                                    and ev.delta.content_delta
                                ):
                                    yield (
                                        "event: delta\ndata: "
                                        + json.dumps({"text": ev.delta.content_delta})
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
                                    yield (
                                        "event: tool\ndata: "
                                        + json.dumps(
                                            {
                                                "name": ev.part.tool_name,
                                                "phase": "end",
                                                "result": str(ev.content)[:_TOOL_RESULT_PREVIEW],
                                            }
                                        )
                                        + "\n\n"
                                    )

                result = run.result
                final_text = result.output if result else ""
                store.add_message(thread_id, "assistant", final_text)
                store.save_history(
                    thread_id, result.all_messages_json().decode() if result else "[]"
                )
                yield "event: done\ndata: {}\n\n"
        except Exception as e:
            logger.exception("chat stream failed for thread %s", thread_id)
            yield (
                "event: error\ndata: "
                + json.dumps({"message": f"{type(e).__name__}: {e}"[:300]})
                + "\n\n"
            )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
