"""Web routes: prowlarr."""

from arrmate.clients.nzbget import NZBgetClient
from arrmate.clients.qbittorrent import QBittorrentClient
from arrmate.clients.sabnzbd import SABnzbdClient
from arrmate.clients.transmission import TransmissionClient

from ._shared import (  # noqa: F401
    Depends,
    Form,
    HTMLResponse,
    Query,
    Request,
    _base_ctx,
    _prowlarr_client,
    auth_router,
    httpx,
    require_power_user,
    router,
    settings,
    sqlite3,
    templates,
)


@router.get("/prowlarr", response_class=HTMLResponse)
async def prowlarr_page(request: Request):
    """Prowlarr indexer search page."""
    client = _prowlarr_client()
    configured = client is not None
    indexers = []
    if client:
        try:
            indexers = await client.get_indexers()
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            pass
        finally:
            await client.close()
    return templates.TemplateResponse(
        request,
        "pages/prowlarr.html",
        {
            **_base_ctx(request),
            "configured": configured,
            "indexers": indexers,
        },
    )


@router.get("/prowlarr/search", response_class=HTMLResponse)
async def prowlarr_search(
    request: Request,
    query: str = Query(default=""),
    categories: str = Query(default=""),
):
    """HTMX partial: Prowlarr indexer search results."""
    client = _prowlarr_client()
    results = []
    error = None

    if not client:
        error = "Prowlarr is not configured (set PROWLARR_URL and PROWLARR_API_KEY)"
    elif query:
        try:
            cat_ids = (
                [int(c) for c in categories.split(",") if c.strip().isdigit()]
                if categories
                else None
            )
            results = await client.search(query, categories=cat_ids)
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            error = str(e)
        finally:
            await client.close()

    return templates.TemplateResponse(
        request,
        "partials/prowlarr_results.html",
        {
            "results": results,
            "query": query,
            "error": error,
            **_base_ctx(request),
        },
    )


@router.post(
    "/prowlarr/send", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def prowlarr_send(
    request: Request,
    url: str = Form(...),
    manager: str = Form(...),
    title: str = Form(default=""),
):
    """Send a Prowlarr search result URL to a configured download manager."""

    try:
        ok = False
        if manager == "sabnzbd" and settings.sabnzbd_url:
            dl_sab = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            ok = await dl_sab.add_url(url)
            await dl_sab.close()
        elif manager == "nzbget" and settings.nzbget_url:
            dl_nzb = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            ok = await dl_nzb.add_url(url)
            await dl_nzb.close()
        elif manager == "qbittorrent" and settings.qbittorrent_url:
            dl_qb = QBittorrentClient(
                str(settings.qbittorrent_url),
                settings.qbittorrent_username or "",
                settings.qbittorrent_password or "",
            )
            ok = await dl_qb.add_url(url)
            await dl_qb.close()
        elif manager == "transmission" and settings.transmission_url:
            dl_tr = TransmissionClient(
                str(settings.transmission_url),
                settings.transmission_username or "",
                settings.transmission_password or "",
            )
            ok = await dl_tr.add_url(url)
            await dl_tr.close()
        label = (title[:60] + "…") if len(title) > 60 else title or url[:60]
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "warning",
                "message": f"Sent to {manager}: {label}" if ok else "Download submitted",
            },
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )
