"""Web routes: pages."""

from arrmate.auth.user_db import set_app_setting
from arrmate.config.service_config import get_service_config

from ._shared import (  # noqa: F401
    _WIZARD_STEPS,
    Depends,
    Form,
    HTMLResponse,
    Query,
    RedirectResponse,
    Request,
    _base_ctx,
    _get_movie_count,
    _get_tv_count,
    asyncio,
    auth_router,
    discover_services,
    httpx,
    logger,
    require_admin,
    reset_parser,
    router,
    save_service_config,
    settings,
    sqlite3,
    templates,
    user_db,
)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Dashboard page with overview."""
    services, tv_count, movie_count = await asyncio.gather(
        discover_services(),
        _get_tv_count(),
        _get_movie_count(),
    )
    available_count = sum(1 for s in services.values() if s.available)

    return templates.TemplateResponse(
        request,
        "pages/index.html",
        {
            **_base_ctx(request),
            "services": services,
            "available_count": available_count,
            "total_count": len(services),
            "tv_count": tv_count,
            "movie_count": movie_count,
        },
    )


@router.get("/services", response_class=HTMLResponse)
async def services_page(request: Request):
    """Services status page."""
    services = await discover_services()

    return templates.TemplateResponse(
        request,
        "pages/services.html",
        {
            **_base_ctx(request),
            "services": services,
        },
    )


@router.get("/help", response_class=HTMLResponse)
async def help_page(request: Request):
    """Help and documentation page."""
    return templates.TemplateResponse(
        request,
        "pages/help.html",
        {
            **_base_ctx(request),
            "version": "0.5.0",
        },
    )


@router.get("/setup", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def setup_wizard(request: Request, step: str = Query(default="welcome")):
    """Setup wizard, shown once after first password change; reachable from the admin panel."""
    valid_steps = [s[0] for s in _WIZARD_STEPS]
    if step not in valid_steps:
        step = "welcome"

    # Gather current service config for pre-filling form fields

    current_cfg = get_service_config()

    return templates.TemplateResponse(
        request,
        "pages/setup_wizard.html",
        {
            "step": step,
            "steps": _WIZARD_STEPS,
            "cfg": current_cfg,
            "settings": settings,
            **_base_ctx(request),
        },
    )


@router.post("/setup/skip", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def setup_wizard_skip(request: Request):
    """Mark setup as complete without configuring anything."""
    user_db.mark_setup_complete()
    return RedirectResponse(url="/web/", status_code=303)


@router.post("/setup/save", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def setup_wizard_save(request: Request, next_step: str = Form(default="done")):
    """Save a wizard step's form data and advance to the next step."""
    try:
        form = await request.form()
        # Exclude the next_step control field; save everything else
        service_data = {k: v for k, v in form.multi_items() if k != "next_step"}
        if service_data:
            save_service_config(service_data)
            reset_parser()
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        logger.error("Setup wizard save failed: %s", e)

    if next_step == "done":
        user_db.mark_setup_complete()

    return RedirectResponse(url=f"/web/setup?step={next_step}", status_code=303)


@router.post(
    "/setup/test-service", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def setup_test_service(request: Request):
    """Test a single service connection and return an inline status badge."""
    import httpx as _httpx

    form = await request.form()
    service = str(form.get("service", ""))
    # Accept either a generic "url" field or the service-specific "<service>_url" field.
    # hx-include sends inputs by their actual name attribute (e.g. "sonarr_url"), not "url".
    url = str(form.get("url", "") or form.get(f"{service}_url", ""))
    api_key = str(
        form.get("api_key", "")
        or form.get(f"{service}_api_key", "")
        or form.get(f"{service}_token", "")
    )

    def _badge(ok: bool, msg: str) -> str:
        colour = "green" if ok else "red"
        icon = "✓" if ok else "✗"
        return (
            f'<span class="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium '
            f'bg-{colour}-900/40 text-{colour}-300 border border-{colour}-700/50">'
            f"{icon} {msg}</span>"
        )

    if not url:
        return HTMLResponse(_badge(False, "No URL configured"))

    url = url.rstrip("/")
    try:
        if service == "ollama":
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{url}/api/tags")
                return HTMLResponse(
                    _badge(
                        r.status_code < 400,
                        "Connected" if r.status_code < 400 else f"HTTP {r.status_code}",
                    )
                )
        elif service in ("sonarr", "radarr", "lidarr", "readarr", "prowlarr"):
            headers = {"X-Api-Key": api_key} if api_key else {}
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{url}/api/v3/system/status", headers=headers)
                return HTMLResponse(
                    _badge(
                        r.status_code == 200,
                        "Connected" if r.status_code == 200 else f"HTTP {r.status_code}",
                    )
                )
        elif service == "plex":
            params = {"X-Plex-Token": api_key} if api_key else {}
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{url}/identity", params=params)
                return HTMLResponse(
                    _badge(
                        r.status_code < 400,
                        "Connected" if r.status_code < 400 else f"HTTP {r.status_code}",
                    )
                )
        elif service == "bazarr":
            headers = {"X-Api-Key": api_key} if api_key else {}
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{url}/api/system/status", headers=headers)
                return HTMLResponse(
                    _badge(
                        r.status_code == 200,
                        "Connected" if r.status_code == 200 else f"HTTP {r.status_code}",
                    )
                )
        elif service == "sabnzbd":
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"{url}/api", params={"output": "json", "mode": "version", "apikey": api_key}
                )
                return HTMLResponse(
                    _badge(
                        r.status_code == 200,
                        "Connected" if r.status_code == 200 else f"HTTP {r.status_code}",
                    )
                )
        elif service in ("qbittorrent", "transmission"):
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(url)
                return HTMLResponse(
                    _badge(
                        r.status_code < 500,
                        "Reachable" if r.status_code < 500 else f"HTTP {r.status_code}",
                    )
                )
        else:
            async with _httpx.AsyncClient(timeout=5) as client:
                r = await client.get(url)
                return HTMLResponse(
                    _badge(
                        r.status_code < 400,
                        "Reachable" if r.status_code < 400 else f"HTTP {r.status_code}",
                    )
                )
    except _httpx.ConnectError:
        return HTMLResponse(_badge(False, "Connection refused"))
    except _httpx.TimeoutException:
        return HTMLResponse(_badge(False, "Timed out"))
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return HTMLResponse(_badge(False, f"Error: {type(e).__name__}"))


@router.post("/setup/reset", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def setup_wizard_reset(request: Request):
    """Reset the setup-complete flag so the wizard can be re-run."""

    set_app_setting("setup_wizard_complete", "0")
    return RedirectResponse(url="/web/setup", status_code=303)


@router.get("/services/refresh", response_class=HTMLResponse)
async def refresh_services(request: Request):
    """Refresh service status and return updated HTML."""
    services = await discover_services()

    return templates.TemplateResponse(
        request,
        "partials/service_list.html",
        {
            "services": services,
        },
    )
