"""Keeping a long thread inside the model's context window.

Tool results are what fill a window here, not conversation: a library listing or an indexer
search is orders of magnitude larger than anything either side says. So compaction empties out
older tool payloads and leaves everything else — the reasoning, the decisions, the user's
instructions — untouched. That keeps the transcript's shape intact, which matters because a
tool call with no matching return is not a valid history to resume from.
"""

import logging

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

logger = logging.getLogger(__name__)

#: Rough bytes-per-token for English JSON. Only used to decide when to act, never reported.
_BYTES_PER_TOKEN = 4

#: Leave a tenth of the window free. The reply being generated and the tool results it pulls in
#: all have to land somewhere, and overflowing costs the whole run.
_BUDGET_FRACTION = 0.9

#: Recent tool output is what the model is actively reasoning about, so it is never stripped.
_KEEP_RECENT_MESSAGES = 12

_STRIPPED = "[older tool output removed to stay within the context window]"


_CANCELLED = "Tool call did not finish: the run was stopped before it returned."


def settle_tool_calls(messages: list[ModelMessage]) -> int:
    """Give a return to every tool call that never got one. Returns how many.

    A run stopped while a tool was still in flight is persisted with that call
    unanswered, and pydantic-ai then rejects every later prompt on the thread with
    "Cannot provide a new user prompt when the message history contains unprocessed
    tool calls" - the conversation is bricked, not merely interrupted. Answering the
    orphans restores a valid call/return alternation and tells the model plainly that
    the tool was cut off rather than leaving it to infer a result it never saw.
    """
    answered = {
        part.tool_call_id
        for msg in messages
        if isinstance(msg, ModelRequest)
        for part in msg.parts
        if isinstance(part, ToolReturnPart)
    }

    settled = 0
    for index in range(len(messages) - 1, -1, -1):
        msg = messages[index]
        if not isinstance(msg, ModelResponse):
            continue
        orphans = [
            part
            for part in msg.parts
            if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
        ]
        if not orphans:
            continue
        # The return has to sit directly after the call it answers, so it is inserted
        # rather than appended: a later message would leave the pairing out of order.
        messages.insert(
            index + 1,
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=part.tool_name,
                        content=_CANCELLED,
                        tool_call_id=part.tool_call_id,
                    )
                    for part in orphans
                ]
            ),
        )
        settled += len(orphans)

    if settled:
        logger.info("settled %d unanswered tool call(s) from an interrupted run", settled)
    return settled


def estimate_tokens(messages: list[ModelMessage]) -> int:
    try:
        from pydantic_ai.messages import ModelMessagesTypeAdapter

        return len(ModelMessagesTypeAdapter.dump_json(messages)) // _BYTES_PER_TOKEN
    except (TypeError, ValueError):
        return 0


def compact(messages: list[ModelMessage], context_window: int) -> tuple[list[ModelMessage], int]:
    """Strip old tool payloads until the history fits the budget.

    Returns the history and how many tool results were emptied, so the caller can say so.
    """
    budget = int(context_window * _BUDGET_FRACTION)
    if not budget or estimate_tokens(messages) <= budget:
        return messages, 0

    stripped = 0
    cutoff = max(0, len(messages) - _KEEP_RECENT_MESSAGES)
    for message in messages[:cutoff]:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, ToolReturnPart) and part.content != _STRIPPED:
                part.content = _STRIPPED
                stripped += 1
        if estimate_tokens(messages) <= budget:
            break

    if stripped:
        logger.info("compacted %d tool results to fit the context window", stripped)
    return messages, stripped
