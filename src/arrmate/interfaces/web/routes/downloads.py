"""Web routes: downloads."""

from typing import Any

from arrmate.clients.nzbget import NZBgetClient
from arrmate.clients.qbittorrent import QBittorrentClient
from arrmate.clients.sabnzbd import SABnzbdClient
from arrmate.clients.transmission import TransmissionClient

from ._shared import (  # noqa: F401
    Depends,
    Form,
    HTMLResponse,
    Request,
    _base_ctx,
    httpx,
    logger,
    require_power_user,
    router,
    settings,
    sqlite3,
    templates,
)


@router.get("/downloads", response_class=HTMLResponse, dependencies=[Depends(require_power_user)])
async def downloads_page(request: Request):
    """Download manager overview page."""
    return templates.TemplateResponse(
        request,
        "pages/downloads.html",
        {**_base_ctx(request)},
    )


@router.get(
    "/downloads/status", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def downloads_status(request: Request):
    """HTMX partial: live download queue from all configured managers."""

    managers: list[dict[str, Any]] = []

    if settings.sabnzbd_url and settings.sabnzbd_api_key:
        sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
        try:
            status = await sab_client.get_status()
            queue = await sab_client.get_queue()
            managers.append(
                {
                    "name": "SABnzbd",
                    "type": "sabnzbd",
                    "status": status,
                    "queue": queue,
                }
            )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            managers.append({"name": "SABnzbd", "type": "sabnzbd", "error": str(e)})
        finally:
            await sab_client.close()

    if settings.nzbget_url and settings.nzbget_username:
        nzb_client = NZBgetClient(
            str(settings.nzbget_url), settings.nzbget_username, settings.nzbget_password or ""
        )
        try:
            status = await nzb_client.get_status()
            queue = await nzb_client.get_queue()
            managers.append({"name": "NZBget", "type": "nzbget", "status": status, "queue": queue})
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            managers.append({"name": "NZBget", "type": "nzbget", "error": str(e)})
        finally:
            await nzb_client.close()

    if settings.qbittorrent_url and settings.qbittorrent_username:
        qb_client = QBittorrentClient(
            str(settings.qbittorrent_url),
            settings.qbittorrent_username,
            settings.qbittorrent_password or "",
        )
        try:
            info = await qb_client.get_transfer_info()
            torrents = await qb_client.get_torrents()
            managers.append(
                {
                    "name": "qBittorrent",
                    "type": "qbittorrent",
                    "status": info,
                    "queue": torrents,
                }
            )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            managers.append({"name": "qBittorrent", "type": "qbittorrent", "error": str(e)})
        finally:
            await qb_client.close()

    if settings.transmission_url:
        tr_client = TransmissionClient(
            str(settings.transmission_url),
            settings.transmission_username or "",
            settings.transmission_password or "",
        )
        try:
            session = await tr_client.get_session()
            tr_torrents = await tr_client.get_torrents()
            managers.append(
                {
                    "name": "Transmission",
                    "type": "transmission",
                    "status": session,
                    "queue": tr_torrents,
                }
            )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            managers.append({"name": "Transmission", "type": "transmission", "error": str(e)})
        finally:
            await tr_client.close()

    return templates.TemplateResponse(
        request,
        "partials/downloads_status.html",
        {"managers": managers, **_base_ctx(request)},
    )


@router.post(
    "/downloads/speed", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def set_download_speed(
    request: Request,
    manager: str = Form(...),
    kbps: int = Form(...),
):
    """Set download speed limit for a download manager."""

    try:
        if manager == "sabnzbd" and settings.sabnzbd_url:
            sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            await sab_client.set_speed_limit(kbps)
            await sab_client.close()
        elif manager == "nzbget" and settings.nzbget_url:
            nzb_client = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            await nzb_client.set_speed_limit(kbps)
            await nzb_client.close()
        elif manager == "qbittorrent" and settings.qbittorrent_url:
            qb_client = QBittorrentClient(
                settings.qbittorrent_url,
                settings.qbittorrent_username or "",
                settings.qbittorrent_password or "",
            )
            await qb_client.set_download_limit(kbps * 1024)
            await qb_client.close()
        elif manager == "transmission" and settings.transmission_url:
            tr_client = TransmissionClient(
                settings.transmission_url,
                settings.transmission_username or "",
                settings.transmission_password or "",
            )
            await tr_client.set_speed_limit_down(kbps)
            await tr_client.close()
        label = "unlimited" if kbps == 0 else f"{kbps} KB/s"
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "success", "message": f"Speed limit set to {label}"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post(
    "/downloads/priority", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def set_download_priority(
    request: Request,
    manager: str = Form(...),
    item_id: str = Form(...),
    priority: int = Form(...),
):
    """Set priority for an individual queue item."""

    try:
        ok = False
        if manager == "sabnzbd" and settings.sabnzbd_url:
            sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            ok = await sab_client.set_priority(item_id, priority)
            await sab_client.close()
        elif manager == "nzbget" and settings.nzbget_url:
            nzb_client = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            ok = await nzb_client.set_priority(int(item_id), priority)
            await nzb_client.close()
        elif manager == "transmission" and settings.transmission_url:
            tr_client = TransmissionClient(
                str(settings.transmission_url),
                settings.transmission_username or "",
                settings.transmission_password or "",
            )
            ok = await tr_client.set_bandwidth_priority(int(item_id), priority)
            await tr_client.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "warning",
                "message": "Priority updated" if ok else "Priority update submitted",
            },
            headers={"HX-Trigger": "downloads-updated"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post(
    "/downloads/move", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def move_download_item(
    request: Request,
    manager: str = Form(...),
    item_id: str = Form(...),
    action: str = Form(...),
):
    """Move a queue item. action: absolute slot for SABnzbd, int offset for NZBget,
    'top'/'bottom'/'increase'/'decrease' for qBittorrent."""

    try:
        ok = False
        if manager == "sabnzbd" and settings.sabnzbd_url:
            sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            ok = await sab_client.move_item(item_id, int(action))
            await sab_client.close()
        elif manager == "nzbget" and settings.nzbget_url:
            nzb_client = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            ok = await nzb_client.move_item(int(item_id), int(action))
            await nzb_client.close()
        elif manager == "qbittorrent" and settings.qbittorrent_url:
            qb_client = QBittorrentClient(
                str(settings.qbittorrent_url),
                settings.qbittorrent_username or "",
                settings.qbittorrent_password or "",
            )
            ok = await qb_client.set_priority(item_id, action)
            await qb_client.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "warning",
                "message": "Queue order updated" if ok else "Queue move submitted",
            },
            headers={"HX-Trigger": "downloads-updated"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post(
    "/downloads/item/pause", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def pause_download_item(
    request: Request,
    manager: str = Form(...),
    item_id: str = Form(...),
):
    """Pause an individual queue item (SABnzbd / NZBget)."""

    try:
        ok = False
        if manager == "sabnzbd" and settings.sabnzbd_url:
            sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            ok = await sab_client.pause_item(item_id)
            await sab_client.close()
        elif manager == "nzbget" and settings.nzbget_url:
            nzb_client = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            ok = await nzb_client.pause_item(int(item_id))
            await nzb_client.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "warning",
                "message": "Item paused" if ok else "Pause submitted",
            },
            headers={"HX-Trigger": "downloads-updated"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post(
    "/downloads/item/resume",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def resume_download_item(
    request: Request,
    manager: str = Form(...),
    item_id: str = Form(...),
):
    """Resume an individual paused queue item (SABnzbd / NZBget)."""

    try:
        ok = False
        if manager == "sabnzbd" and settings.sabnzbd_url:
            sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            ok = await sab_client.resume_item(item_id)
            await sab_client.close()
        elif manager == "nzbget" and settings.nzbget_url:
            nzb_client = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            ok = await nzb_client.resume_item(int(item_id))
            await nzb_client.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "warning",
                "message": "Item resumed" if ok else "Resume submitted",
            },
            headers={"HX-Trigger": "downloads-updated"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post(
    "/downloads/add", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def add_download(
    request: Request,
    manager: str = Form(...),
    url: str = Form(...),
    priority: int = Form(default=0),
    category: str = Form(default=""),
):
    """Add an NZB or torrent/magnet URL directly to a download manager."""

    try:
        ok = False
        if manager == "sabnzbd" and settings.sabnzbd_url:
            sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
            ok = await sab_client.add_url(url, priority=priority, category=category)
            await sab_client.close()
        elif manager == "nzbget" and settings.nzbget_url:
            nzb_client = NZBgetClient(
                str(settings.nzbget_url),
                settings.nzbget_username or "",
                settings.nzbget_password or "",
            )
            ok = await nzb_client.add_url(url, priority=priority, category=category)
            await nzb_client.close()
        elif manager == "qbittorrent" and settings.qbittorrent_url:
            qb_client = QBittorrentClient(
                str(settings.qbittorrent_url),
                settings.qbittorrent_username or "",
                settings.qbittorrent_password or "",
            )
            ok = await qb_client.add_url(url, category=category)
            await qb_client.close()
        elif manager == "transmission" and settings.transmission_url:
            tr_client = TransmissionClient(
                str(settings.transmission_url),
                settings.transmission_username or "",
                settings.transmission_password or "",
            )
            ok = await tr_client.add_url(url)
            await tr_client.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "warning",
                "message": "Download added" if ok else "Download submitted",
            },
            headers={"HX-Trigger": "downloads-updated"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )
