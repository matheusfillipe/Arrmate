"""Natural language command parser using LLM."""

from arrmate.llm.base import BaseLLMProvider
from arrmate.llm.factory import create_llm_provider
from arrmate.llm.schemas import get_system_prompt, get_tool_schemas

from .models import Intent


class CommandParser:
    """Parses natural language commands into structured Intent objects."""

    def __init__(
        self,
        llm_provider: BaseLLMProvider | None = None,
        available_services: list[str] | None = None,
    ) -> None:
        """Initialize the command parser.

        Args:
            llm_provider: Optional LLM provider (creates default if not provided)
            available_services: List of service names that are configured and
                reachable. Used to build a service-aware system prompt so the
                LLM knows which media types and operations are actually available.
        """
        self.llm_provider = llm_provider or create_llm_provider()
        self.available_services = available_services

    async def parse(self, user_input: str) -> Intent:
        """Parse a natural language command into structured intent.

        Args:
            user_input: User's natural language command

        Returns:
            Parsed Intent object

        Raises:
            ConversationalReply: If the model answered in prose
            ValueError: If parsing fails or intent is invalid
        """
        return await self.llm_provider.parse_command(
            user_input, get_tool_schemas(), get_system_prompt(self.available_services)
        )

    async def close(self) -> None:
        """Clean up resources."""
        if self.llm_provider:
            await self.llm_provider.close()
