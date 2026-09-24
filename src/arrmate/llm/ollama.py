"""Ollama LLM provider implementation."""

import re

import httpx
import ollama
from pydantic import ValidationError

from arrmate.core.models import Intent

from .base import BaseLLMProvider, ConversationalReply
from .schemas import ToolSchema

_FENCED_JSON = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON = re.compile(r"\{[^{}]*\"action\"[^{}]*\}", re.DOTALL)

# The client reports an unreachable server as a plain ConnectionError, an OSError.
_OLLAMA_ERRORS = (ollama.ResponseError, ollama.RequestError, httpx.HTTPError, OSError, ValueError)


def _ollama_tool(tool: ToolSchema) -> ollama.Tool:
    return ollama.Tool(
        type="function",
        function=ollama.Tool.Function.model_validate(tool.model_dump()),
    )


class OllamaProvider(BaseLLMProvider):
    """Ollama LLM provider with tool calling support."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
    ) -> None:
        """Initialize Ollama provider.

        Args:
            model: Ollama model to use (must support tool calling).
                Recommended: qwen2.5:7b, llama3.1:8b, mistral-nemo:12b
            base_url: Ollama server base URL
            api_key: Optional bearer token for authenticated Ollama instances
        """
        super().__init__(model)
        self.base_url = base_url
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        self.client = ollama.Client(host=base_url, headers=headers)

    def supports_tool_calling(self) -> bool:
        """Ollama supports tool calling with compatible models."""
        return True

    async def parse_command(
        self, user_input: str, tools: list[ToolSchema], system_prompt: str
    ) -> Intent:
        """Parse command using Ollama with tool calling."""
        try:
            response = self.client.chat(
                model=self.model or "qwen2.5:7b",
                messages=[
                    ollama.Message(role="system", content=system_prompt),
                    ollama.Message(role="user", content=user_input),
                ],
                tools=[_ollama_tool(tool) for tool in tools],
            )

            message = response.message
            tool_calls = message.tool_calls or []
            if tool_calls and tool_calls[0].function.arguments:
                return Intent.model_validate(tool_calls[0].function.arguments)

            # Some models answer with the JSON in the text instead of calling the tool.
            content = message.content or ""
            if content:
                extracted = self._extract_intent_from_text(content)
                if extracted:
                    return extracted
                raise ConversationalReply(content)

            raise ValueError(
                "LLM did not use the parse_media_command function and no "
                "structured data could be extracted from the response"
            )

        except _OLLAMA_ERRORS as e:
            raise ValueError(f"Failed to parse command with Ollama: {e!s}") from e

    def _extract_intent_from_text(self, text: str) -> Intent | None:
        """Read an intent from a ```json fence, or failing that a bare object with an action."""
        for pattern, group in ((_FENCED_JSON, 1), (_BARE_JSON, 0)):
            match = pattern.search(text)
            if match:
                try:
                    return Intent.model_validate_json(match.group(group))
                except ValidationError:
                    continue
        return None
