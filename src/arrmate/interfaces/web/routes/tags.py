"""Web routes: tags."""

from collections import Counter

from pydantic import BaseModel

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


class TagCount(BaseModel):
    id: int
    label: str
    count: int


async def _tag_counts(client: SonarrClient | RadarrClient) -> list[TagCount]:
    """Every tag of the service with the number of library items carrying it."""
    try:
        tags = await client.get_tags()
        usage = Counter(tag_id for item in await client.get_all_items() for tag_id in item.tags)
    finally:
        await client.close()
    return [TagCount(id=tag.id, label=tag.label, count=usage[tag.id]) for tag in tags]


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
    tags: list[TagCount] = []
    error = None
    try:
        if service == "sonarr" and settings.sonarr_url and settings.sonarr_api_key:
            tags = await _tag_counts(
                SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            )
        elif service == "radarr" and settings.radarr_url and settings.radarr_api_key:
            tags = await _tag_counts(
                RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            )
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
