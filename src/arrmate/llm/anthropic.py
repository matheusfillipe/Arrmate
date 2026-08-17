"""Anthropic (Claude) LLM provider implementation."""

from typing import Any, cast

from anthropic import AsyncAnthropic

from .base import BaseLLMProvider, ConversationalReply


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude LLM provider with tool use support."""

    def __init__(
        self,
        model: str = "claude-3-5-sonnet-20241022",
        api_key: str | None = None,
    ) -> None:
        """Initialize Anthropic provider.

        Args:
            model: Claude model to use
            api_key: Anthropic API key
        """
        super().__init__(model)
        self.client = AsyncAnthropic(api_key=api_key)

    def supports_tool_calling(self) -> bool:
        """Anthropic supports tool use."""
        return True

    async def parse_command(
        self, user_input: str, tools: list[dict[str, Any]], system_prompt: str
    ) -> dict[str, Any]:
        """Parse command using Claude with tool use.

        Args:
            user_input: User's natural language command
            tools: Tool schemas for tool use
            system_prompt: System prompt

        Returns:
            Parsed parameters from tool use

        Raises:
            ValueError: If parsing fails
        """
        try:
            # Anthropic tool format
            anthropic_tools = [
                {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["parameters"],
                }
                for tool in tools
            ]

            response = await self.client.messages.create(
                model=self.model or "claude-3-5-sonnet-20241022",
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_input}],
                tools=cast("list[Any]", anthropic_tools),
            )

            # Extract tool use from response
            tool_use_block = None
            for block in response.content:
                if block.type == "tool_use":
                    tool_use_block = block
                    break

            if not tool_use_block:
                prose = "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()
                if prose:
                    raise ConversationalReply(prose)
                raise ValueError("Claude did not use the parse_media_command tool")

            function_args = tool_use_block.input

            if not function_args:
                raise ValueError("No input returned from tool use")

            return function_args

        except ConversationalReply:
            raise
        except Exception as e:
            raise ValueError(f"Failed to parse command with Anthropic: {e!s}") from e

    async def close(self) -> None:
        """Close the Anthropic client."""
        await self.client.close()
