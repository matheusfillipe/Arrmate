"""MCP servers exposed to the agent as tools.

Arrmate ships the tools every install needs; anything specific to one deployment
belongs in an MCP server that deployment runs itself. Each configured server is
wrapped so the same guarantees the built-in tools give still hold: a mutating call
is refused unless the caller's role allows writes, and every call is recorded before
its effects are known.

The write gate lives here rather than in the server because it depends on who is
asking, which is Arrmate's knowledge, not the server's.
"""

import logging
import time
import uuid
from typing import Any

from pydantic_ai.mcp import CallToolFunc, MCPToolset, ProcessToolCallback
from pydantic_ai.tools import RunContext
from pydantic_ai.toolsets import AbstractToolset

from arrmate.config.settings import MCPServerConfig, settings

from .deps import AgentDeps

logger = logging.getLogger(__name__)

#: Tools name their own dry-run flag this way, matching the playbook convention:
#: deterministic code decides how, the model decides only when, and a human approves
#: the version that writes.
APPLY_ARG = "apply"


def _is_mutating(args: dict[str, Any]) -> bool:
    return bool(args.get(APPLY_ARG))


def _build_processor(server: MCPServerConfig) -> ProcessToolCallback:
    async def process_tool_call(
        ctx: RunContext[AgentDeps],
        call_tool: CallToolFunc,
        name: str,
        args: dict[str, Any],
    ) -> Any:
        request_id = uuid.uuid4().hex[:12]
        mutating = _is_mutating(args)

        if mutating:
            # Raises PermissionError, which the agent surfaces to the model as a
            # refusal rather than a crash.
            ctx.deps.require_write(f"{server.id}.{name}")

        if server.inject_request_id:
            args = {**args, "request_id": request_id}

        logger.info(
            "mcp call start server=%s tool=%s request_id=%s user=%s apply=%s args=%s",
            server.id,
            name,
            request_id,
            ctx.deps.username,
            mutating,
            args,
        )
        started = time.monotonic()
        try:
            result = await call_tool(name, args)
        except Exception as e:
            logger.warning(
                "mcp call failed server=%s tool=%s request_id=%s elapsed=%.2fs %s: %s",
                server.id,
                name,
                request_id,
                time.monotonic() - started,
                type(e).__name__,
                e,
            )
            raise
        logger.info(
            "mcp call ok server=%s tool=%s request_id=%s elapsed=%.2fs",
            server.id,
            name,
            request_id,
            time.monotonic() - started,
        )
        return result

    return process_tool_call


def build_mcp_toolsets() -> list[AbstractToolset[AgentDeps]]:
    """Build a toolset per enabled MCP server.

    A server that cannot be reached must cost Arrmate its tools and nothing else, so
    construction never raises: the agent is cached process-wide, and an exception here
    would leave every request without a chat at all.
    """
    toolsets: list[AbstractToolset[AgentDeps]] = []
    for server in settings.mcp_servers:
        if not server.enabled:
            continue
        headers = {"Authorization": f"Bearer {server.token}"} if server.token else None
        try:
            toolsets.append(
                MCPToolset(
                    server.url,
                    id=server.id,
                    headers=headers,
                    process_tool_call=_build_processor(server),
                    init_timeout=server.timeout_seconds,
                    read_timeout=server.timeout_seconds,
                )
            )
        except (ValueError, TypeError, OSError) as e:
            logger.warning("MCP server %s not usable: %s: %s", server.id, type(e).__name__, e)
            continue
        logger.info("MCP server %s registered (%s)", server.id, server.url)
    return toolsets
