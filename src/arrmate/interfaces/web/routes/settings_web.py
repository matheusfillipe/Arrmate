"""Web routes: settings_web."""

from ._shared import (  # noqa: F401
    Depends,
    Form,
    HTMLResponse,
    Request,
    _base_ctx,
    auth_manager,
    auth_router,
    clear_session_cookie,
    create_session_token,
    httpx,
    logger,
    require_admin,
    reset_parser,
    router,
    save_service_config,
    set_session_cookie,
    settings,
    sqlite3,
    templates,
)


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def settings_page(request: Request):
    """Settings page — admin only."""
    return templates.TemplateResponse(
        request,
        "pages/settings.html",
        {
            **_base_ctx(request),
            "settings": settings,
        },
    )


@router.post(
    "/settings/auth/set", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def auth_set(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    """Set or replace auth credentials."""
    error = None
    if len(username.strip()) < 1:
        error = "Username is required"
    elif len(password) < 8:
        error = "Password must be at least 8 characters"
    elif password != password_confirm:
        error = "Passwords do not match"

    if error:
        return templates.TemplateResponse(
            request,
            "partials/auth_settings.html",
            {"auth_error": error},
        )

    auth_manager.set_credentials(username.strip(), password)

    # Set session cookie so the user stays logged in
    token = create_session_token("legacy", username.strip(), "admin", auth_manager.get_secret_key())
    response = templates.TemplateResponse(
        request,
        "partials/auth_settings.html",
        {"auth_success": "Authentication enabled"},
    )
    set_session_cookie(response, token)
    return response


@router.post(
    "/settings/auth/enable", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def auth_enable(request: Request):
    """Re-enable auth."""
    auth_manager.enable()

    username = auth_manager.get_username()
    if username:
        token = create_session_token("legacy", username, "admin", auth_manager.get_secret_key())
        response = templates.TemplateResponse(
            request,
            "partials/auth_settings.html",
            {"auth_success": "Authentication re-enabled"},
        )
        set_session_cookie(response, token)
        return response

    return templates.TemplateResponse(
        request,
        "partials/auth_settings.html",
        {"auth_success": "Authentication re-enabled"},
    )


@router.post(
    "/settings/auth/disable", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def auth_disable(request: Request):
    """Disable auth without deleting credentials."""
    auth_manager.disable()
    return templates.TemplateResponse(
        request,
        "partials/auth_settings.html",
        {"auth_success": "Authentication disabled"},
    )


@router.post(
    "/settings/auth/delete", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def auth_delete(request: Request):
    """Delete all credentials."""
    auth_manager.delete()
    response = templates.TemplateResponse(
        request,
        "partials/auth_settings.html",
        {"auth_success": "Credentials deleted"},
    )
    clear_session_cookie(response)
    return response


@router.post(
    "/settings/auth/plex-sso", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def save_plex_sso_settings(request: Request):
    """Save Plex SSO configuration from the Auth settings panel."""
    from arrmate.config.service_config import save_service_config

    form = await request.form()
    save_service_config(
        {
            "plex_sso_enabled": form.get("plex_sso_enabled", ""),
            "plex_sso_default_role": str(form.get("plex_sso_default_role", "user")),
            "plex_sso_require_approval": form.get("plex_sso_require_approval", ""),
            "plex_sso_verify_plex_friends": form.get("plex_sso_verify_plex_friends", ""),
        }
    )
    return templates.TemplateResponse(
        request,
        "partials/auth_settings.html",
        {
            "settings": settings,
            "auth_success": "Plex SSO settings saved.",
            **_base_ctx(request),
        },
    )


@router.post(
    "/settings/services", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def save_services(request: Request):
    """Save service URLs and API keys to persistent config."""
    try:
        form = await request.form()
        save_service_config(dict(form.multi_items()))
        reset_parser()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "success", "message": "Settings saved"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        logger.error("Failed to save service config: %s", e)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": f"Failed to save: {e}"},
        )


@router.post(
    "/settings/notifications/test/slack",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def test_slack_webhook(request: Request):
    """Send a test Slack notification."""
    from arrmate.auth.notifications import send_slack

    if not settings.slack_webhook_url:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "No Slack webhook configured"},
        )
    ok = send_slack(settings.slack_webhook_url, "Arrmate test notification", title="Test")
    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {
            "type": "success" if ok else "error",
            "message": "Slack test sent!" if ok else "Slack test failed — check webhook URL",
        },
    )


@router.post(
    "/settings/notifications/test/discord",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def test_discord_webhook(request: Request):
    """Send a test Discord notification."""
    from arrmate.auth.notifications import send_discord

    if not settings.discord_webhook_url:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "No Discord webhook configured"},
        )
    ok = send_discord(settings.discord_webhook_url, "Arrmate test notification", title="Test")
    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {
            "type": "success" if ok else "error",
            "message": "Discord test sent!" if ok else "Discord test failed — check webhook URL",
        },
    )
