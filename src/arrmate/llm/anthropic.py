"""Anthropic (Claude) LLM provider implementation."""

from anthropic import AnthropicError, AsyncAnthropic
from anthropic.types import ToolParam

from arrmate.core.models import Intent

from .base import BaseLLMProvider, ConversationalReply
from .schemas import ToolSchema


def _anthropic_tool(tool: ToolSchema) -> ToolParam:
    return ToolParam(
        name=tool.name, description=tool.description, input_schema=dict(tool.parameters)
    )


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
        self, user_input: str, tools: list[ToolSchema], system_prompt: str
    ) -> Intent:
        """Parse command using Claude with tool use."""
        try:
            response = await self.client.messages.create(
                model=self.model or "claude-3-5-sonnet-20241022",
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_input}],
                tools=[_anthropic_tool(tool) for tool in tools],
            )

            tool_use_block = next(
                (block for block in response.content if block.type == "tool_use"), None
            )
            if not tool_use_block:
                prose = "".join(
                    block.text for block in response.content if block.type == "text"
                ).strip()
                if prose:
                    raise ConversationalReply(prose)
                raise ValueError("Claude did not use the parse_media_command tool")

            return Intent.model_validate(tool_use_block.input)

        except (AnthropicError, ValueError) as e:
            raise ValueError(f"Failed to parse command with Anthropic: {e!s}") from e

    async def close(self) -> None:
        """Close the Anthropic client."""
        await self.client.close()
