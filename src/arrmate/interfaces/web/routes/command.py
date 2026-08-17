"""Web routes: command."""

from ._shared import (  # noqa: F401
    DESTRUCTIVE_ACTIONS,
    USER_BLOCKED_ACTIONS,
    ActionType,
    Form,
    HTMLResponse,
    Request,
    _base_ctx,
    auth_router,
    get_current_user,
    get_engine,
    get_executor,
    get_parser,
    httpx,
    logger,
    router,
    sqlite3,
    templates,
)


@router.get("/command", response_class=HTMLResponse)
async def command_page(request: Request):
    """Command input page."""
    return templates.TemplateResponse(
        request,
        "pages/command.html",
        {
            **_base_ctx(request),
        },
    )


@router.post("/command/parse", response_class=HTMLResponse)
async def parse_command(request: Request, command: str = Form(...)):
    """Parse command and return preview HTML."""
    try:
        cmd_parser = await get_parser()
        intent = await cmd_parser.parse(command)

        return templates.TemplateResponse(
            request,
            "partials/command_preview.html",
            {
                "intent": intent,
                "command": command,
            },
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "partials/command_preview.html",
            {
                "error": str(e),
                "command": command,
            },
        )


@router.post("/command/execute", response_class=HTMLResponse)
async def execute_command(
    request: Request,
    command: str = Form(...),
    mode: str = Form(default=""),
    confirmed: str = Form(default=""),
):
    """Execute command and return result HTML with toast.

    mode: optional override — "transcode" to run FFmpeg instead of Sonarr/Radarr search.
    'user' role cannot execute REMOVE or DELETE actions.
    """
    current_user = get_current_user(request)
    user_role = current_user.get("role", "user") if current_user else "user"

    # Outer guard: catches template-rendering failures so HTMX always gets a 200 with
    # visible HTML instead of a silent 500 that leaves the result area blank.
    try:
        try:
            # Parse
            cmd_parser = await get_parser()
            intent = await cmd_parser.parse(command)

            # If the user explicitly chose transcode mode, override the action
            if mode == "transcode":
                intent.action = ActionType.TRANSCODE

            # Role gate runs before enrichment so it never depends on
            # service availability.
            if user_role == "user" and intent.action in USER_BLOCKED_ACTIONS:
                return templates.TemplateResponse(
                    request,
                    "partials/execution_result.html",
                    {
                        "result": {
                            "success": False,
                            "message": "You don't have permission to remove or delete media. "
                            "Submit a request instead.",
                            "errors": ["Insufficient permissions for this action"],
                        },
                        "show_toast": True,
                        "toast_type": "error",
                        "toast_message": "Permission denied: cannot remove media",
                    },
                )

            # Enrich (after the role gate)
            intent_engine = get_engine()
            enriched = await intent_engine.enrich(intent)

            # Validate
            errors = intent_engine.validate(enriched)
            if errors:
                return templates.TemplateResponse(
                    request,
                    "partials/execution_result.html",
                    {
                        "result": {
                            "success": False,
                            "message": "Validation failed",
                            "errors": errors,
                        },
                        "show_toast": True,
                        "toast_type": "error",
                        "toast_message": "Validation failed: " + "; ".join(errors),
                    },
                )

            # Require explicit confirmation for destructive actions (admin/power_user only)
            if enriched.action in DESTRUCTIVE_ACTIONS and confirmed != "true":
                title = enriched.title or "this item"
                if enriched.episodes and enriched.season:
                    ep_str = ", ".join(f"E{e:02d}" for e in sorted(enriched.episodes))
                    delete_description = f"{title} - Season {enriched.season} ({ep_str})"
                elif enriched.season:
                    delete_description = f"{title} - Season {enriched.season}"
                else:
                    delete_description = title
                return templates.TemplateResponse(
                    request,
                    "partials/delete_confirm.html",
                    {
                        "delete_description": delete_description,
                        "command": command,
                        "mode": mode,
                    },
                )

            # Execute
            exec_engine = get_executor()
            result = await exec_engine.execute(enriched)

            return templates.TemplateResponse(
                request,
                "partials/execution_result.html",
                {
                    "result": result,
                    "original_command": command,
                    "show_toast": True,
                    "toast_type": "success" if result.success else "error",
                    "toast_message": result.message,
                },
            )

        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            logger.exception("Unhandled error in execute_command for %r", command)
            raw = str(e)
            if "400" in raw:
                friendly = (
                    "The service rejected the request (HTTP 400); the item may already exist."
                )
            elif any(c in raw for c in ("401", "403")):
                friendly = "Authentication failed — check your API key in Settings."
            elif any(c in raw for c in ("502", "503", "504")):
                friendly = "A service is temporarily unavailable. Try again in a moment."
            elif any(kw in raw.lower() for kw in ("connection", "connect", "timed out", "timeout")):
                friendly = "Could not reach a service; check that it is running and its URL."
            elif "failed to parse" in raw.lower():
                friendly = "The AI had trouble understanding that request. Try rephrasing it."
            else:
                friendly = f"Something went wrong: {raw}"

            return templates.TemplateResponse(
                request,
                "partials/execution_result.html",
                {
                    "result": {
                        "success": False,
                        "message": friendly,
                        "errors": [raw],
                    },
                    "original_command": command,
                    "show_toast": True,
                    "toast_type": "error",
                    "toast_message": friendly,
                },
            )

    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        import traceback

        tb = traceback.format_exc()
        logger.exception("Fatal error rendering execute_command response for %r", command)
        safe_tb = tb.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return HTMLResponse(
            content=f"""<div class="card border-red-700/50 p-4">
  <div class="flex gap-3 items-start">
    <span class="text-2xl flex-shrink-0">❌</span>
    <div class="flex-1 min-w-0">
      <p class="text-red-400 font-semibold mb-1">Internal server error</p>
      <p class="text-gray-300 text-sm">An unexpected error occurred while processing.</p>
      <details class="mt-3">
        <summary class="text-xs text-gray-500 cursor-pointer hover:text-gray-400
          select-none">▶ Show technical details</summary>
        <pre class="mt-2 text-xs text-gray-400 bg-gray-900 p-3 rounded overflow-x-auto
      whitespace-pre-wrap border border-gray-700/50">{safe_tb}</pre>
      </details>
    </div>
  </div>
</div>""",
            status_code=200,
        )
