"""Web routes: downloads."""

from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import TypeAdapter

from arrmate.clients import nzbget, qbittorrent, sabnzbd, transmission
from arrmate.clients.nzbget import NZBgetClient
from arrmate.clients.qbittorrent import QBittorrentClient, QueueMove
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

ManagerKind = Literal["sabnzbd", "nzbget", "qbittorrent", "transmission"]
_QUEUE_MOVE: TypeAdapter[QueueMove] = TypeAdapter(QueueMove)


@dataclass(frozen=True)
class PanelError:
    name: str
    type: ManagerKind
    error: str


@dataclass(frozen=True)
class SabnzbdPanel:
    status: sabnzbd.Queue
    queue: list[sabnzbd.QueueSlot]
    name: ClassVar[str] = "SABnzbd"
    type: ClassVar[ManagerKind] = "sabnzbd"


@dataclass(frozen=True)
class NzbgetPanel:
    status: nzbget.Status
    queue: list[nzbget.Group]
    name: ClassVar[str] = "NZBget"
    type: ClassVar[ManagerKind] = "nzbget"


@dataclass(frozen=True)
class QbittorrentPanel:
    status: qbittorrent.TransferInfo
    queue: list[qbittorrent.Torrent]
    name: ClassVar[str] = "qBittorrent"
    type: ClassVar[ManagerKind] = "qbittorrent"


@dataclass(frozen=True)
class TransmissionPanel:
    status: transmission.Session
    queue: list[transmission.Torrent]
    name: ClassVar[str] = "Transmission"
    type: ClassVar[ManagerKind] = "transmission"


DownloadPanel = PanelError | SabnzbdPanel | NzbgetPanel | QbittorrentPanel | TransmissionPanel


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

    managers: list[DownloadPanel] = []

    if settings.sabnzbd_url and settings.sabnzbd_api_key:
        sab_client = SABnzbdClient(str(settings.sabnzbd_url), str(settings.sabnzbd_api_key))
        try:
            queue = await sab_client.get_queue()
            managers.append(SabnzbdPanel(status=queue, queue=queue.slots))
        except (httpx.HTTPError, ValueError) as e:
            managers.append(PanelError(name="SABnzbd", type="sabnzbd", error=str(e)))
        finally:
            await sab_client.close()

    if settings.nzbget_url and settings.nzbget_username:
        nzb_client = NZBgetClient(
            str(settings.nzbget_url), settings.nzbget_username, settings.nzbget_password or ""
        )
        try:
            managers.append(
                NzbgetPanel(
                    status=await nzb_client.get_status(), queue=await nzb_client.get_queue()
                )
            )
        except (httpx.HTTPError, ValueError) as e:
            managers.append(PanelError(name="NZBget", type="nzbget", error=str(e)))
        finally:
            await nzb_client.close()

    if settings.qbittorrent_url and settings.qbittorrent_username:
        qb_client = QBittorrentClient(
            str(settings.qbittorrent_url),
            settings.qbittorrent_username,
            settings.qbittorrent_password or "",
        )
        try:
            managers.append(
                QbittorrentPanel(
                    status=await qb_client.get_transfer_info(),
                    queue=await qb_client.get_torrents(),
                )
            )
        except (httpx.HTTPError, ValueError) as e:
            managers.append(PanelError(name="qBittorrent", type="qbittorrent", error=str(e)))
        finally:
            await qb_client.close()

    if settings.transmission_url:
        tr_client = TransmissionClient(
            str(settings.transmission_url),
            settings.transmission_username or "",
            settings.transmission_password or "",
        )
        try:
            managers.append(
                TransmissionPanel(
                    status=await tr_client.get_session(), queue=await tr_client.get_torrents()
                )
            )
        except (httpx.HTTPError, ValueError) as e:
            managers.append(PanelError(name="Transmission", type="transmission", error=str(e)))
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
    manager: ManagerKind = Form(...),
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
    manager: ManagerKind = Form(...),
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
    manager: ManagerKind = Form(...),
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
            ok = await qb_client.set_priority(item_id, _QUEUE_MOVE.validate_python(action))
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
    manager: ManagerKind = Form(...),
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
    manager: ManagerKind = Form(...),
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
    manager: ManagerKind = Form(...),
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
