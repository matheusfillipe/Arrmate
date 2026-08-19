"""MCP toolset wiring: the write gate, request-id injection, and failure isolation."""

import pytest

from arrmate.agent.deps import ROLE_ADMIN, ROLE_USER, AgentDeps
from arrmate.agent.mcp_toolsets import _build_processor, build_mcp_toolsets
from arrmate.config.settings import MCPServerConfig, settings


class _Ctx:
    def __init__(self, deps: AgentDeps):
        self.deps = deps


def _server(**kw) -> MCPServerConfig:
    return MCPServerConfig(id="media", url="http://media-mcp:8080/mcp", **kw)


@pytest.mark.asyncio
async def test_read_call_allowed_for_plain_user():
    calls: list[tuple[str, dict]] = []

    async def call_tool(name, args):
        calls.append((name, args))
        return "ok"

    process = _build_processor(_server())
    ctx = _Ctx(AgentDeps(user_id="1", username="matheus", role=ROLE_USER))

    result = await process(ctx, call_tool, "list_games", {"platform": "ps4"})

    assert result == "ok"
    assert calls[0][0] == "list_games"


@pytest.mark.asyncio
async def test_apply_call_refused_for_plain_user():
    async def call_tool(name, args):  # pragma: no cover - must not run
        raise AssertionError("tool ran despite the role gate")

    process = _build_processor(_server())
    ctx = _Ctx(AgentDeps(user_id="1", username="matheus", role=ROLE_USER))

    result = await process(ctx, call_tool, "cleanup_sources", {"path": "/data", "apply": True})
    assert result["error"] == "permission-denied"


@pytest.mark.asyncio
async def test_apply_call_allowed_for_admin():
    async def call_tool(name, args):
        return "deleted"

    process = _build_processor(_server())
    ctx = _Ctx(AgentDeps(user_id="1", username="matheus", role=ROLE_ADMIN))

    assert await process(ctx, call_tool, "cleanup_sources", {"apply": True}) == "deleted"


@pytest.mark.asyncio
async def test_request_id_injected_and_can_be_disabled():
    seen: list[dict] = []

    async def call_tool(name, args):
        seen.append(args)
        return "ok"

    ctx = _Ctx(AgentDeps(user_id="1", username="matheus", role=ROLE_ADMIN))

    await _build_processor(_server())(ctx, call_tool, "list_games", {})
    assert len(seen[0]["request_id"]) == 12

    await _build_processor(_server(inject_request_id=False))(ctx, call_tool, "list_games", {})
    assert "request_id" not in seen[1]


@pytest.mark.asyncio
async def test_failure_is_logged_and_reraised():
    async def call_tool(name, args):
        raise TimeoutError("server gone")

    process = _build_processor(_server())
    ctx = _Ctx(AgentDeps(user_id="1", username="matheus", role=ROLE_ADMIN))

    with pytest.raises(TimeoutError):
        await process(ctx, call_tool, "list_games", {})


def test_disabled_server_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "mcp_servers", [_server(enabled=False)])
    assert build_mcp_toolsets() == []


def test_enabled_server_builds_a_toolset(monkeypatch):
    monkeypatch.setattr(settings, "mcp_servers", [_server(token="secret")])
    toolsets = build_mcp_toolsets()
    assert len(toolsets) == 1
