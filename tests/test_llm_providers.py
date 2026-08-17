"""Tests for LLM provider parsing behaviour."""

from types import SimpleNamespace

import pytest

from arrmate.llm.base import ConversationalReply
from arrmate.llm.openai import OpenAIProvider

TOOLS = [
    {
        "name": "parse_media_command",
        "description": "Parse a media command",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    }
]


def _provider(message: SimpleNamespace) -> OpenAIProvider:
    """A provider whose completions call returns one canned assistant message."""
    p = OpenAIProvider(model="test-model", api_key="k", base_url="http://llm.test/v1")

    async def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    p.client.chat.completions.create = create
    return p


@pytest.mark.asyncio
async def test_prose_answer_is_not_a_parse_failure():
    """Small talk gets a reply, not an error; the model understood, it just had no command."""
    p = _provider(SimpleNamespace(tool_calls=None, content="Hi! Ask me to add or list media."))

    with pytest.raises(ConversationalReply) as excinfo:
        await p.parse_command("hello", TOOLS, "system")

    assert "Ask me to add or list media" in excinfo.value.text


@pytest.mark.asyncio
async def test_no_tool_call_and_no_text_is_a_parse_failure():
    p = _provider(SimpleNamespace(tool_calls=None, content=None))

    with pytest.raises(ValueError, match="did not use the parse_media_command"):
        await p.parse_command("hello", TOOLS, "system")


@pytest.mark.asyncio
async def test_tool_call_is_parsed():
    call = SimpleNamespace(
        function=SimpleNamespace(name="parse_media_command", arguments='{"action": "list"}')
    )
    p = _provider(SimpleNamespace(tool_calls=[call], content=None))

    assert await p.parse_command("list my shows", TOOLS, "system") == {"action": "list"}
