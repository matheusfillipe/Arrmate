"""OpenAI LLM provider implementation."""

import json
from typing import Any, cast

from openai import AsyncOpenAI

from .base import BaseLLMProvider, ConversationalReply


class OpenAIProvider(BaseLLMProvider):
    """OpenAI LLM provider with function calling support."""

    def __init__(
        self,
        model: str = "gpt-4-turbo-preview",
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        """Initialize OpenAI provider.

        Args:
            model: OpenAI model to use
            api_key: OpenAI API key
            base_url: Optional custom base URL
        """
        super().__init__(model)
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    def supports_tool_calling(self) -> bool:
        """OpenAI supports function calling."""
        return True

    async def parse_command(
        self, user_input: str, tools: list[dict[str, Any]], system_prompt: str
    ) -> dict[str, Any]:
        """Parse command using OpenAI with function calling.

        Args:
            user_input: User's natural language command
            tools: Tool schemas for function calling
            system_prompt: System prompt

        Returns:
            Parsed parameters from function call

        Raises:
            ValueError: If parsing fails
        """
        try:
            # OpenAI function calling format
            openai_tools = [{"type": "function", "function": tool} for tool in tools]

            response = await self.client.chat.completions.create(
                model=self.model or "gpt-4-turbo-preview",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input},
                ],
                tools=cast("list[Any]", openai_tools),
                tool_choice="auto",
            )

            message = response.choices[0].message

            if not message.tool_calls:
                if message.content:
                    raise ConversationalReply(message.content)
                raise ValueError("LLM did not use the parse_media_command function")

            # Get the first tool call
            tool_call = message.tool_calls[0]
            if not hasattr(tool_call, "function"):
                raise ValueError("LLM returned a custom tool call; function call required")
            function_args = json.loads(tool_call.function.arguments)

            if not function_args:
                raise ValueError("No arguments returned from function call")

            args: dict[str, Any] = function_args
            return args

        except ConversationalReply:
            raise
        except Exception as e:
            raise ValueError(f"Failed to parse command with OpenAI: {e!s}") from e

    async def close(self) -> None:
        """Close the OpenAI client."""
        await self.client.close()
