"""Abstract base class for LLM providers."""

from abc import ABC, abstractmethod
from typing import Any


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
        self, user_input: str, tools: list[dict[str, Any]], system_prompt: str
    ) -> dict[str, Any]:
        """Parse a natural language command using tool calling.

        Args:
            user_input: The user's natural language command
            tools: List of tool/function schemas
            system_prompt: System prompt for the LLM

        Returns:
            Dictionary with parsed intent parameters

        Raises:
            ValueError: If parsing fails or LLM doesn't use tools correctly
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
