"""Abstract base class for LLM providers."""

from abc import ABC, abstractmethod

from arrmate.core.models import Intent

from .schemas import ToolSchema


class ConversationalReply(Exception):
    """The model answered in prose instead of calling the command tool.

    Small talk and questions are not parse failures, so callers should show
    ``text`` to the user rather than an error.
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class BaseLLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, model: str | None = None) -> None:
        """Initialize the provider with optional model override.

        Args:
            model: Model name to use (provider-specific)
        """
        self.model = model

    @abstractmethod
    async def parse_command(
        self, user_input: str, tools: list[ToolSchema], system_prompt: str
    ) -> Intent:
        """Parse a natural language command using tool calling.

        Raises:
            ConversationalReply: If the model replied in prose instead of calling the tool
            ValueError: If the call fails or the tool arguments are not a valid intent
        """

    @abstractmethod
    def supports_tool_calling(self) -> bool:
        """Check if this provider supports native tool/function calling.

        Returns:
            True if tool calling is supported, False otherwise
        """

    async def close(self) -> None:
        """Clean up any resources; the default provider holds none."""
        return
