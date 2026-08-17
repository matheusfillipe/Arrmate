"""Web routes: tags."""

from ._shared import (  # noqa: F401
    Depends,
    Form,
    HTMLResponse,
    Query,
    RadarrClient,
    Request,
    SonarrClient,
    _base_ctx,
    auth_router,
    httpx,
    require_power_user,
    router,
    settings,
    sqlite3,
    templates,
)


@router.get("/tags", response_class=HTMLResponse, dependencies=[Depends(require_power_user)])
async def tags_page(request: Request):
    """Tag management page for Sonarr and Radarr."""
    sonarr_configured = bool(settings.sonarr_url and settings.sonarr_api_key)
    radarr_configured = bool(settings.radarr_url and settings.radarr_api_key)
    return templates.TemplateResponse(
        request,
        "pages/tags.html",
        {
            **_base_ctx(request),
            "sonarr_configured": sonarr_configured,
            "radarr_configured": radarr_configured,
        },
    )


@router.get("/tags/list", response_class=HTMLResponse, dependencies=[Depends(require_power_user)])
async def tags_list(
    request: Request,
    service: str = Query(default="radarr"),
):
    """HTMX partial: list tags for a service with item counts."""
    tags = []
    error = None
    try:
        if service == "sonarr" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr_client = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                raw_tags = await sonarr_client.get_tags()
                all_series = await sonarr_client.get_all_items()
                # Build a lookup: tag_id -> count of series using it
                tag_counts: dict = {}
                for s in all_series:
                    for tid in s.get("tags", []):
                        tag_counts[tid] = tag_counts.get(tid, 0) + 1
                tags = [
                    {"id": t["id"], "label": t["label"], "count": tag_counts.get(t["id"], 0)}
                    for t in raw_tags
                ]
            finally:
                await sonarr_client.close()
        elif service == "radarr" and settings.radarr_url and settings.radarr_api_key:
            radarr_client = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                raw_tags = await radarr_client.get_tags()
                all_movies = await radarr_client.get_all_items()
                movie_tag_counts: dict = {}
                for m in all_movies:
                    for tid in m.get("tags", []):
                        movie_tag_counts[tid] = movie_tag_counts.get(tid, 0) + 1
                tags = [
                    {"id": t["id"], "label": t["label"], "count": movie_tag_counts.get(t["id"], 0)}
                    for t in raw_tags
                ]
            finally:
                await radarr_client.close()
        else:
            error = f"{service.capitalize()} is not configured"
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        error = str(e)

    return templates.TemplateResponse(
        request,
        "partials/tags_list.html",
        {
            "tags": tags,
            "service": service,
            "error": error,
        },
    )


@router.post(
    "/tags/create", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def tags_create(
    request: Request,
    label: str = Form(...),
    service: str = Form(...),
):
    """Create a new tag in Sonarr or Radarr."""
    error = None
    try:
        label = label.strip().lower()
        if not label:
            error = "Tag name cannot be empty"
        elif service == "sonarr" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr_client_2 = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
            try:
                await sonarr_client_2.create_tag(label)
            finally:
                await sonarr_client_2.close()
        elif service == "radarr" and settings.radarr_url and settings.radarr_api_key:
            radarr_client_2 = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                await radarr_client_2.create_tag(label)
            finally:
                await radarr_client_2.close()
        else:
            error = f"{service.capitalize()} is not configured"
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        error = str(e)

    if error:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": error},
        )

    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {
            "type": "success",
            "message": f"Tag '{label}' created in {service.capitalize()}",
        },
        headers={"HX-Trigger": f"tags-updated-{service}"},
    )


@router.delete(
    "/tags/{service}/{tag_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def tags_delete(
    request: Request,
    service: str,
    tag_id: int,
):
    """Delete a tag from Sonarr or Radarr."""
    error = None
    try:
        if service == "sonarr" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr_client_3 = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                await sonarr_client_3.delete_tag(tag_id)
            finally:
                await sonarr_client_3.close()
        elif service == "radarr" and settings.radarr_url and settings.radarr_api_key:
            radarr_client_3 = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                await radarr_client_3.delete_tag(tag_id)
            finally:
                await radarr_client_3.close()
        else:
            error = f"{service.capitalize()} is not configured"
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        error = str(e)

    if error:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": error},
        )

    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {
            "type": "success",
            "message": "Tag deleted",
        },
        headers={"HX-Trigger": f"tags-updated-{service}"},
    )
