"""FastAPI REST API interface for Arrmate."""

import asyncio
import logging
import sqlite3
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

try:
    _VERSION = _pkg_version("arrmate")
except PackageNotFoundError:
    _VERSION = "1.0.0"

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from arrmate.agent import store as chat_store
from arrmate.agent.chat import router as chat_router
from arrmate.auth import user_db
from arrmate.auth.dependencies import AuthRedirectException, get_api_user
from arrmate.auth.rate_limit import login_limiter
from arrmate.clients.discovery import discover_services
from arrmate.config.service_config import apply_saved_config
from arrmate.config.settings import settings
from arrmate.core.command_parser import CommandParser
from arrmate.core.download_tracker import run_tracker
from arrmate.core.executor import Executor
from arrmate.core.intent_engine import IntentEngine
from arrmate.core.models import USER_BLOCKED_ACTIONS, EnhancedServiceInfo, ExecutionResult
from arrmate.interfaces.web.routes import auth_router
from arrmate.interfaces.web.routes import router as web_router

logger = logging.getLogger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()


# ── Request / Response models ─────────────────────────────────────────────────


class TokenLoginRequest(BaseModel):
    """Login with username + password to obtain an API token."""

    username: str = Field(..., description="Arrmate username")
    password: str = Field(..., description="Account password")
    token_name: str = Field(default="API Token", description="Friendly name for this token")
    expires_days: int | None = Field(
        default=None,
        description="Token lifetime in days (omit or null for no expiry)",
    )


class TokenLoginResponse(BaseModel):
    token: str = Field(description="Bearer token — store securely, shown only once")
    token_id: str
    username: str
    role: str
    expires_at: str | None = None
    note: str = "Store this token now — it will not be shown again."


class CommandRequest(BaseModel):
    """Request model for command execution."""

    command: str = Field(..., description="Natural language command to execute", max_length=2000)
    dry_run: bool = Field(default=False, description="Parse only, don't execute")


class CommandResponse(BaseModel):
    """Response model for command execution."""

    success: bool
    message: str
    intent: dict
    result: ExecutionResult | None = None


class UserInfo(BaseModel):
    user_id: str
    username: str
    role: str


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Arrmate API",
    description=(
        "Natural language interface for media management.\n\n"
        "**Authentication**: All `/api/v1/*` endpoints require a Bearer token.\n"
        "Create tokens at `/web/api-tokens` or via `POST /api/v1/auth/token`."
    ),
    version=_VERSION,
)

# Mount static files
static_dir = Path(__file__).parent.parent / "web" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# Exception handler for auth redirects
@app.exception_handler(AuthRedirectException)
async def auth_redirect_handler(request: Request, exc: AuthRedirectException) -> Response:
    if exc.is_htmx:
        if exc.login_url == "/web/change-password":
            return Response(status_code=204)
        return Response(status_code=200, headers={"HX-Redirect": exc.login_url})
    return RedirectResponse(url=exc.login_url, status_code=303)


# Include web routers
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(chat_router)

# Global components (initialized on startup)
parser: CommandParser | None = None
engine: IntentEngine | None = None
executor: Executor | None = None


@app.on_event("startup")
async def startup_event() -> None:
    global parser, engine, executor
    apply_saved_config()
    try:
        user_db.init_db()
    except sqlite3.Error as e:
        logging.getLogger(__name__).warning("user_db init failed: %s", e)
    try:
        chat_store.init_db()
    except sqlite3.Error as e:
        logging.getLogger(__name__).warning("chat store init failed: %s", e)
    services = await discover_services()
    available = [name for name, info in services.items() if info.available]
    parser = CommandParser(available_services=available or None)
    engine = IntentEngine()
    executor = Executor()

    # Warn if transcoding is possible but path restrictions aren't configured
    if (settings.sonarr_url or settings.radarr_url) and not settings.transcode_allowed_roots:
        logger.warning(
            "TRANSCODE_ALLOWED_ROOTS is not set. Transcoding will accept any file path "
            "reported by Sonarr/Radarr. Set this to limit transcoding to your media directories."
        )

    # Start background download tracker (polls Sonarr/Radarr every 5 min)
    tracker_task = asyncio.create_task(run_tracker())
    _background_tasks.add(tracker_task)
    tracker_task.add_done_callback(_background_tasks.discard)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    if parser:
        await parser.close()


# ── Public endpoints ──────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/web/")


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    """Health check — no auth required."""
    return {"status": "ok", "version": _VERSION}


# ── Auth endpoints ────────────────────────────────────────────────────────────


@app.post("/api/v1/auth/token", response_model=TokenLoginResponse, tags=["auth"])
async def login_for_token(req: TokenLoginRequest, request: Request) -> TokenLoginResponse:
    """Exchange username + password for a long-lived API Bearer token.

    The token is shown **only once** in the response — store it securely.
    Subsequent requests must include `Authorization: Bearer <token>`.
    """
    allowed, retry_after = await login_limiter.check(login_limiter._get_client_ip(request))
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    db_user = user_db.verify_user(req.username, req.password)
    if not db_user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if not db_user.get("enabled"):
        raise HTTPException(status_code=403, detail="Account is disabled")

    token_id, plain_token = user_db.create_api_token(
        user_id=db_user["id"],
        name=req.token_name,
        expires_days=req.expires_days,
    )
    # Fetch the record to get expires_at
    tokens = user_db.list_api_tokens(db_user["id"])
    record = next((t for t in tokens if t["id"] == token_id), {})

    return TokenLoginResponse(
        token=plain_token,
        token_id=token_id,
        username=db_user["username"],
        role=db_user["role"],
        expires_at=record.get("expires_at"),
    )


@app.delete("/api/v1/auth/token", status_code=204, tags=["auth"])
async def revoke_current_token(user: dict = Depends(get_api_user)) -> None:
    """Revoke the token used to make this request (logout for API clients)."""
    user_db.delete_api_token(user["token_id"], user["user_id"])


# ── User endpoints ────────────────────────────────────────────────────────────


@app.get("/api/v1/user", response_model=UserInfo, tags=["user"])
async def get_current_user(user: dict = Depends(get_api_user)) -> UserInfo:
    """Return the authenticated user's info."""
    return UserInfo(
        user_id=user["user_id"],
        username=user["username"],
        role=user["role"],
    )


# ── Core endpoints ────────────────────────────────────────────────────────────


@app.post("/api/v1/execute", response_model=CommandResponse, tags=["commands"])
async def execute_command(
    request: CommandRequest,
    user: dict = Depends(get_api_user),
) -> CommandResponse:
    """Execute a natural language media command.

    Roles:
    - **admin / power_user**: full access
    - **user**: read-only commands only (destructive actions blocked)
    """
    if parser is None or engine is None or executor is None:
        raise HTTPException(status_code=503, detail="Service is still starting up")

    try:
        intent = await parser.parse(request.command)

        # Role gate runs before enrichment so it never depends on
        # service availability.
        if intent.action in USER_BLOCKED_ACTIONS and user.get("role") == "user":
            raise HTTPException(
                status_code=403,
                detail="Your role does not allow destructive commands",
            )

        if request.dry_run:
            return CommandResponse(
                success=True,
                message="Command parsed successfully (dry run)",
                intent=intent.model_dump(),
                result=None,
            )

        enriched_intent = await engine.enrich(intent)
        errors = engine.validate(enriched_intent)
        if errors:
            raise HTTPException(
                status_code=400,
                detail={"message": "Validation failed", "errors": errors},
            )

        result = await executor.execute(enriched_intent)
        return CommandResponse(
            success=result.success,
            message=result.message,
            intent=enriched_intent.model_dump(),
            result=result,
        )

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Unhandled error in execute_command: %s", e)
        raise HTTPException(status_code=500, detail="An internal error occurred") from e


@app.get(
    "/api/v1/services",
    response_model=dict[str, EnhancedServiceInfo],
    tags=["services"],
)
async def get_services(user: dict = Depends(get_api_user)) -> dict[str, EnhancedServiceInfo]:
    """Get status of all configured media services."""
    try:
        return await discover_services()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/v1/config", tags=["meta"])
async def get_config(user: dict = Depends(get_api_user)) -> dict[str, str]:
    """Get current application configuration (sanitized, no secrets)."""
    config = {
        "llm_provider": settings.llm_provider,
        "log_level": settings.log_level,
        "api_port": str(settings.api_port),
    }
    if settings.llm_provider == "ollama":
        config["ollama_url"] = settings.ollama_base_url
        config["ollama_model"] = settings.ollama_model
    elif settings.llm_provider == "openai":
        config["openai_model"] = settings.openai_model
    elif settings.llm_provider == "anthropic":
        config["anthropic_model"] = settings.anthropic_model
    return config


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )
