"""OpenAI LLM provider implementation."""

from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionFunctionToolParam
from openai.types.shared_params import FunctionDefinition

from arrmate.core.models import Intent

from .base import BaseLLMProvider, ConversationalReply
from .schemas import ToolSchema


def _openai_tool(tool: ToolSchema) -> ChatCompletionFunctionToolParam:
    return ChatCompletionFunctionToolParam(
        type="function",
        function=FunctionDefinition(
            name=tool.name, description=tool.description, parameters=dict(tool.parameters)
        ),
    )


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
        self, user_input: str, tools: list[ToolSchema], system_prompt: str
    ) -> Intent:
        """Parse command using OpenAI with function calling."""
        try:
            response = await self.client.chat.completions.create(
                model=self.model or "gpt-4-turbo-preview",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input},
                ],
                tools=[_openai_tool(tool) for tool in tools],
                tool_choice="auto",
            )

            message = response.choices[0].message

            if not message.tool_calls:
                if message.content:
                    raise ConversationalReply(message.content)
                raise ValueError("LLM did not use the parse_media_command function")

            tool_call = message.tool_calls[0]
            if not hasattr(tool_call, "function"):
                raise ValueError("LLM returned a custom tool call; function call required")
            return Intent.model_validate_json(tool_call.function.arguments)

        except (OpenAIError, ValueError) as e:
            raise ValueError(f"Failed to parse command with OpenAI: {e!s}") from e

    async def close(self) -> None:
        """Close the OpenAI client."""
        await self.client.close()
