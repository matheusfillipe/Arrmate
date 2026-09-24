"""Factory for creating LLM provider instances."""

from typing import assert_never

from arrmate.config.settings import settings

from .anthropic import AnthropicProvider
from .base import BaseLLMProvider
from .ollama import OllamaProvider
from .openai import OpenAIProvider


def create_llm_provider() -> BaseLLMProvider:
    """Create the LLM provider configured in settings.

    Raises:
        ValueError: If the provider's API key is missing
    """
    match settings.llm_provider:
        case "ollama":
            return OllamaProvider(
                model=settings.ollama_model,
                base_url=settings.ollama_base_url,
                api_key=settings.ollama_api_key,
            )
        case "openai":
            if not settings.openai_api_key:
                raise ValueError("OPENAI_API_KEY is required for OpenAI provider")
            return OpenAIProvider(
                model=settings.openai_model,
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url,
            )
        case "anthropic":
            if not settings.anthropic_api_key:
                raise ValueError("ANTHROPIC_API_KEY is required for Anthropic provider")
            return AnthropicProvider(
                model=settings.anthropic_model,
                api_key=settings.anthropic_api_key,
            )
        case _:
            assert_never(settings.llm_provider)
