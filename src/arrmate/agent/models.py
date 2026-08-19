"""Agent construction: pydantic-ai Agent built from Arrmate settings."""

import logging
from functools import lru_cache

from pydantic_ai import Agent, RunContext
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.ollama import OllamaModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.ollama import OllamaProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from arrmate.config.settings import settings

from .deps import AgentDeps
from .mcp_toolsets import build_mcp_toolsets
from .playbooks import register_playbook_tools
from .system_prompt import build_system_prompt
from .tools import register_tools

logger = logging.getLogger(__name__)

MAX_TOOL_CALLS_PER_RUN = 1000

#: A long diagnosis/repair run is steerable and stoppable (see chat.py), so the ceiling only
#: needs to stop a genuinely runaway loop, not a normal multi-step task.
RUN_DEADLINE_SECONDS = 3 * 60 * 60

#: Applied per-run (Agent.iter(usage_limits=...)) — the limit is a property
#: of a conversation turn, not of the agent.
RUN_USAGE_LIMITS = UsageLimits(tool_calls_limit=MAX_TOOL_CALLS_PER_RUN)


def _build_model() -> Model:
    provider = settings.llm_provider

    if provider == "ollama":
        # pydantic-ai's OllamaProvider rides on the OpenAI client, which
        # appends /chat/completions itself; Ollama's compat endpoint lives
        # under /v1, so the base URL must end with it.
        base_url = settings.ollama_base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        return OllamaModel(
            settings.ollama_model,
            provider=OllamaProvider(base_url=base_url, api_key=settings.ollama_api_key or None),
        )

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("ANTHROPIC_API_KEY is required for the anthropic provider")
        return AnthropicModel(
            settings.anthropic_model,
            provider=AnthropicProvider(api_key=settings.anthropic_api_key),
        )

    # "openai" also covers any OpenAI-compatible endpoint (Groq, OpenRouter,
    # z.ai coding plan, LM Studio, ...) via OPENAI_BASE_URL.
    if not settings.openai_api_key:
        raise ValueError("OPENAI_API_KEY is required for the openai provider")
    return OpenAIChatModel(
        settings.openai_model,
        provider=OpenAIProvider(
            base_url=settings.openai_base_url,
            api_key=settings.openai_api_key,
        ),
    )


@lru_cache(maxsize=1)
def get_agent() -> Agent[AgentDeps, str]:
    """Build (once) the chat agent from current settings.

    The Agent instance is cached per process; a settings change that alters
    the provider requires a restart (get_agent.cache_clear() on save).
    """
    mcp_toolsets = build_mcp_toolsets()
    agent: Agent[AgentDeps, str] = Agent(
        _build_model(),
        deps_type=AgentDeps,
        output_type=str,
        toolsets=mcp_toolsets,
    )

    @agent.system_prompt
    async def _dynamic_system_prompt(ctx: RunContext[AgentDeps]) -> str:
        return await build_system_prompt()

    register_tools(agent)
    register_playbook_tools(agent)
    logger.info(
        "Chat agent built (provider=%s, mcp_servers=%d)",
        settings.llm_provider,
        len(mcp_toolsets),
    )
    return agent
