"""Ollama LLM provider implementation."""

import json
import re
from typing import Any

import ollama

from .base import BaseLLMProvider, ConversationalReply


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
        self, user_input: str, tools: list[dict[str, Any]], system_prompt: str
    ) -> dict[str, Any]:
        """Parse command using Ollama with tool calling.

        Args:
            user_input: User's natural language command
            tools: Tool schemas for function calling
            system_prompt: System prompt

        Returns:
            Parsed parameters from tool call

        Raises:
            ValueError: If parsing fails
        """
        try:
            # Ollama tool calling format
            ollama_tools = [
                {
                    "type": "function",
                    "function": tool,
                }
                for tool in tools
            ]

            response = self.client.chat(
                model=self.model or "qwen2.5:7b",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input},
                ],
                tools=ollama_tools,
            )

            # Extract tool call from response
            # The ollama library returns typed objects (not dicts)
            message = response.message
            tool_calls = message.tool_calls or []

            if tool_calls:
                # Get the first tool call (should be parse_media_command)
                tool_call = tool_calls[0]
                # function.arguments is already a dict in the ollama library
                function_args = tool_call.function.arguments

                if function_args:
                    return dict(function_args)

            # Fallback: try to extract JSON from the text response
            content = message.content or ""
            if content:
                extracted = self._extract_json_from_text(content)
                if extracted:
                    return extracted
                raise ConversationalReply(content)

            raise ValueError(
                "LLM did not use the parse_media_command function and no "
                "structured data could be extracted from the response"
            )

        except ConversationalReply:
            raise
        except Exception as e:
            raise ValueError(f"Failed to parse command with Ollama: {e!s}") from e

    def _extract_json_from_text(self, text: str) -> dict[str, Any] | None:
        """Try to extract structured command data from a text response.

        Some models respond with JSON in the text instead of using tool calls.
        This attempts to find and parse that JSON.
        """
        # Try to find JSON object in the text
        # Look for ```json ... ``` blocks first
        json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                if "action" in data and "media_type" in data:
                    parsed: dict[str, Any] = data
                    return parsed
            except json.JSONDecodeError:
                pass

        # Try to find a bare JSON object
        json_match = re.search(r"\{[^{}]*\"action\"[^{}]*\}", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                if "action" in data and "media_type" in data:
                    bare: dict[str, Any] = data
                    return bare
            except json.JSONDecodeError:
                pass

        return None
