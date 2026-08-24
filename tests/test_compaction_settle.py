"""A run stopped mid-tool must not brick the thread it was running in."""

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from arrmate.agent.compaction import settle_tool_calls


def _call(cid: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name="list_games", args={}, tool_call_id=cid)])


def _ret(cid: str) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name="list_games", content="[]", tool_call_id=cid)]
    )


def test_answers_a_call_that_never_returned():
    msgs = [ModelRequest(parts=[UserPromptPart(content="hi")]), _call("a1")]

    assert settle_tool_calls(msgs) == 1

    tail = msgs[-1]
    assert isinstance(tail, ModelRequest)
    part = tail.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_call_id == "a1"
    assert "stopped" in part.content


def test_leaves_a_complete_history_alone():
    msgs = [ModelRequest(parts=[UserPromptPart(content="hi")]), _call("a1"), _ret("a1")]
    before = list(msgs)

    assert settle_tool_calls(msgs) == 0
    assert msgs == before


def test_return_is_inserted_directly_after_its_call():
    """A return placed at the end instead would leave the pairing out of order."""
    msgs = [_call("a1"), ModelResponse(parts=[TextPart(content="done")])]

    assert settle_tool_calls(msgs) == 1

    assert isinstance(msgs[1], ModelRequest)
    assert msgs[1].parts[0].tool_call_id == "a1"
    assert isinstance(msgs[2], ModelResponse)


def test_settles_several_orphans_in_one_response():
    msgs = [
        ModelResponse(
            parts=[
                ToolCallPart(tool_name="a", args={}, tool_call_id="x"),
                ToolCallPart(tool_name="b", args={}, tool_call_id="y"),
            ]
        )
    ]

    assert settle_tool_calls(msgs) == 2
    assert {p.tool_call_id for p in msgs[1].parts} == {"x", "y"}
