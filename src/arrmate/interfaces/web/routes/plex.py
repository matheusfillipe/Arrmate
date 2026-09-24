"""Web routes: plex."""

import time
from typing import Literal, NamedTuple

from pydantic import BaseModel

from arrmate.auth.models import UserRole
from arrmate.cache import plex_cache
from arrmate.clients.plex import (
    PlexAccount,
    PlexMedia,
    PlexMetadata,
    PlexPlayer,
    PlexSession,
    PlexSessionUser,
    PlexTranscodeSession,
)

from ._shared import (  # noqa: F401
    Depends,
    Form,
    HTMLResponse,
    PlexClient,
    Query,
    Request,
    Response,
    _base_ctx,
    _plex_client,
    _plex_client_for_user,
    _plex_thumb_url,
    _plex_tv_client,
    asyncio,
    auth_router,
    get_current_user,
    httpx,
    logger,
    require_power_user,
    router,
    settings,
    sqlite3,
    templates,
)


class ButlerTaskInfo(NamedTuple):
    name: str
    label: str
    desc: str


BUTLER_TASKS = [
    ButlerTaskInfo("CleanOldBundles", "Clean Old Bundles", "Remove unused bundle data"),
    ButlerTaskInfo("CleanOldCacheFiles", "Clean Cache Files", "Delete stale cached files"),
    ButlerTaskInfo("BackupDatabase", "Backup Database", "Back up the Plex database"),
    ButlerTaskInfo("DeepMediaAnalysis", "Deep Media Analysis", "Re-analyse loudness & bitrate"),
    ButlerTaskInfo("RefreshLocalMedia", "Refresh Local Media", "Scan for local metadata/artwork"),
    ButlerTaskInfo("SearchForSubtitles", "Search for Subtitles", "Find missing subtitle files"),
    ButlerTaskInfo("GenerateAutoTags", "Generate Auto Tags", "Auto-tag music files"),
    ButlerTaskInfo("UpgradeMediaAnalysis", "Upgrade Media Analysis", "Update media analysis data"),
    ButlerTaskInfo(
        "GenerateChapterImageThumbnails", "Chapter Thumbnails", "Generate chapter image thumbnails"
    ),
    ButlerTaskInfo(
        "ScanAndAnalyzeFiles", "Scan & Analyze Files", "Scan all files and run media analysis"
    ),
    ButlerTaskInfo(
        "GenerateIntroVideoMarkers", "Detect Intros", "Detect intro sequences across all libraries"
    ),
    ButlerTaskInfo(
        "GenerateEndCreditsMarkers", "Detect Credits", "Detect end-credit sequences (PlexPass)"
    ),
    ButlerTaskInfo(
        "GenerateMediaIndexFiles",
        "Generate Index Files",
        "Generate media index files for faster seeking",
    ),
    ButlerTaskInfo(
        "RecheckPendingIntroVideoMarkers",
        "Recheck Intro Markers",
        "Re-check pending intro detection tasks",
    ),
]


class AccountOption(BaseModel):
    id: int
    title: str


class HomeUserOption(BaseModel):
    id: int
    title: str
    thumb: str | None = None


class HistoryRow(BaseModel):
    title: str
    subtitle: str
    type: str
    viewed_at: int
    thumb: str | None = None
    rating_key: str | None = None
    user: str


class MediaCard(BaseModel):
    title: str
    subtitle: str
    type: str
    thumb: str | None = None
    rating_key: str | None = None
    pct: int | None = None
    summary: str | None = None


class TitleGroup(BaseModel):
    title: str
    kind: Literal["tv", "movie"]
    thumb: str | None = None
    count: int = 0
    last_watched: int = 0
    account_ids: set[int] = set()

    @property
    def user_count(self) -> int:
        return len(self.account_ids)


class ButlerRow(BaseModel):
    name: str
    label: str
    desc: str
    running: bool
    enabled: bool


class PlaylistRow(BaseModel):
    id: str | None = None
    title: str
    playlist_type: str
    item_count: int
    duration: str
    thumb: str | None = None
    summary: str


class SessionRow(BaseModel):
    title: str
    subtitle: str
    type: str
    pct: int
    user: str
    user_thumb: str
    player: str
    platform: str
    state: str
    location: str
    bandwidth: str
    video_decision: str
    audio_decision: str
    src_video: str
    src_audio: str
    src_res: str
    dst_video: str
    dst_audio: str
    session_id: str
    thumb: str | None = None


class ShareLibrary(BaseModel):
    key: int
    title: str
    type: str


class ShareFriend(BaseModel):
    id: int
    username: str
    email: str
    thumb: str
    all_libraries: bool
    section_titles: list[str]


class NowPlayingRow(BaseModel):
    title: str
    user: str
    player: str
    state: str
    pct: int
    type: str
    session_key: str
    session_id: str


def _thumb(item: PlexMetadata) -> str | None:
    path = item.any_thumb
    return _plex_thumb_url(path) if path else None


def _year_label(item: PlexMetadata) -> str:
    return str(item.year) if item.year else ""


def _episode_card(item: PlexMetadata) -> MediaCard:
    """Card for an in-progress, on-deck or recently added item."""
    match item.type:
        case "episode":
            title = item.grandparent_title or item.title or "Unknown"
            subtitle = f"{item.episode_code} - {item.title or ''}"
        case "season":
            title = item.parent_title or item.title or "Unknown"
            subtitle = item.title or ""
        case _:
            title = item.title or "Unknown"
            subtitle = _year_label(item)
    return MediaCard(
        title=title,
        subtitle=subtitle,
        type=item.type or "",
        thumb=_thumb(item),
        rating_key=item.rating_key,
    )


def _title_kind(plex_type: str) -> Literal["tv", "movie"]:
    return "tv" if plex_type in ("episode", "season") else "movie"


def _match_account_id(accounts: list[PlexAccount], username: str) -> int:
    """Plex account id whose name matches an Arrmate username; 0 when none does."""
    target = username.lower()
    return next((a.id for a in accounts if a.display_name.lower() == target), 0)


@router.get("/plex", response_class=HTMLResponse)
async def plex_page(request: Request):
    """Plex hub page."""
    plex = _plex_client()
    configured = plex is not None
    accounts = []
    home_users = []
    raw_accounts = []
    if plex:
        try:
            raw_accounts = [a for a in await plex.get_accounts() if a.id != 0]
            accounts = [AccountOption(id=a.id, title=a.display_name) for a in raw_accounts]
        except (httpx.HTTPError, ValueError):
            pass
        finally:
            await plex.close()
    tv = _plex_tv_client()
    if tv:
        try:
            home_users = [
                HomeUserOption(
                    id=u.id, title=u.title or u.username or f"User {u.id}", thumb=u.thumb
                )
                for u in await tv.get_home_users()
                if not u.admin
            ]
        except (httpx.HTTPError, ValueError):
            pass
        finally:
            await tv.close()

    current_user = get_current_user(request)
    viewer_account_id: int | None = None
    if current_user and current_user.role == UserRole.USER:
        viewer_account_id = _match_account_id(raw_accounts, current_user.username) or None

    return templates.TemplateResponse(
        request,
        "pages/plex.html",
        {
            **_base_ctx(request),
            "configured": configured,
            "accounts": accounts,
            "home_users": home_users,
            "butler_tasks": BUTLER_TASKS,
            "viewer_account_id": viewer_account_id,
        },
    )


@router.get("/plex/thumb", response_class=HTMLResponse)
async def plex_thumb(path: str = Query(...)):
    """Proxy a Plex thumbnail image (keeps token server-side)."""
    if not settings.plex_url or not settings.plex_token:
        return Response(status_code=404)
    url = f"{settings.plex_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as hx:
            resp = await hx.get(
                url,
                headers={
                    "X-Plex-Token": settings.plex_token,
                    "Accept": "image/jpeg,image/*",
                },
            )
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "image/jpeg")
                return Response(content=resp.content, media_type=ct)
    except httpx.HTTPError:
        pass
    return Response(status_code=404)


@router.get("/plex/history", response_class=HTMLResponse)
async def plex_history(
    request: Request,
    account_id: int = Query(default=0),
    days: int = Query(default=7, ge=0),  # 0 = all time
):
    """HTMX partial: watch history."""
    cutoff = int(time.time()) - (days * 86400) if days > 0 else 0
    # Fetch more items when a short window is selected so we don't miss entries
    fetch_limit = 500 if days > 0 else 200

    current_user = get_current_user(request)
    items = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            accounts = await plex.get_accounts()
            account_names = {a.id: a.display_name for a in accounts}
            account_names.setdefault(1, "Main User")

            # Regular users can only see their own history, so we enforce it server-side.
            if current_user and current_user.role == UserRole.USER:
                account_id = _match_account_id(accounts, current_user.username)
                # A user with no matching account gets a sentinel that matches no history.
                if account_id == 0:
                    account_id = -1

            raw = await plex.get_history(
                account_id=account_id,
                limit=fetch_limit,
                min_date=cutoff if cutoff else None,
            )
            for item in raw:
                viewed_at = item.viewed_at or 0
                if viewed_at == 0 or (cutoff and viewed_at < cutoff):
                    continue
                if item.type == "episode":
                    show = item.grandparent_title or item.title or ""
                    ep_title = item.title or ""
                    title = f"{show} — {ep_title}" if show else ep_title
                    subtitle = item.episode_code
                else:
                    title = item.title or ""
                    subtitle = _year_label(item)
                # Media removed from the library leaves history entries with no title.
                if not title:
                    continue
                items.append(
                    HistoryRow(
                        title=title,
                        subtitle=subtitle,
                        type=item.type or "",
                        viewed_at=viewed_at,
                        thumb=_plex_thumb_url(item.thumb) if item.thumb else None,
                        rating_key=item.rating_key,
                        user=account_names.get(item.account_id or 0, ""),
                    )
                )
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_history.html",
        {"items": items, "error": error, "days": days},
    )


async def _plex_account_id_for_username(username: str) -> int:
    """Match an Arrmate username to a Plex home account id; 0 when no match."""
    plex = _plex_client()
    if not plex:
        return 0
    try:
        accounts = await plex.get_accounts()
    except (httpx.HTTPError, ValueError):
        logger.warning("failed to list Plex accounts for account match", exc_info=True)
        return 0
    finally:
        await plex.close()
    return _match_account_id(accounts, username)


async def _plex_client_scoped(
    request: Request, user_id: int
) -> tuple[PlexClient | None, str | None]:
    """Plex client for a household view; user role is locked to their own account."""
    current_user = get_current_user(request)
    if current_user and current_user.role == UserRole.USER:
        matched = await _plex_account_id_for_username(current_user.username)
        if matched == 0:
            return None, "No Plex account matches your username."
        return await _plex_client_for_user(matched), None
    return await _plex_client_for_user(user_id), None


@router.get("/plex/continue", response_class=HTMLResponse)
async def plex_continue_watching(
    request: Request,
    user_id: int = Query(default=0),
):
    """HTMX partial: continue watching list."""
    items = []
    plex, error = await _plex_client_scoped(request, user_id)
    if plex:
        try:
            items = [
                _episode_card(item).model_copy(update={"pct": item.progress_pct})
                for item in await plex.get_continue_watching()
            ]
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_continue.html",
        {"items": items, "error": error},
    )


@router.get("/plex/ondeck", response_class=HTMLResponse)
async def plex_on_deck(
    request: Request,
    user_id: int = Query(default=0),
):
    """HTMX partial: on deck items."""
    items = []
    plex, error = await _plex_client_scoped(request, user_id)
    if plex:
        try:
            items = [
                _episode_card(item).model_copy(update={"summary": (item.summary or "")[:120]})
                for item in await plex.get_on_deck()
            ]
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_ondeck.html",
        {"items": items, "error": error},
    )


@router.get("/plex/recent", response_class=HTMLResponse)
async def plex_recently_added(
    request: Request,
    limit: int = Query(default=25, le=100),
):
    """HTMX partial: recently added items."""
    items = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            items = [_episode_card(item) for item in await plex.get_recently_added(limit=limit)]
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_recent.html",
        {"items": items, "error": error},
    )


@router.get("/plex/bytitle", response_class=HTMLResponse)
async def plex_by_title(
    request: Request,
    bt_search: str = Query(default=""),
    bt_letter: str = Query(default=""),
    bt_media_type: str = Query(default="all"),
):
    """HTMX partial: watch history grouped and sorted by title (served from local cache)."""

    error = None

    if plex_cache.is_stale():
        plex = _plex_client()
        if plex:
            try:
                plex_cache.populate_cache(await plex.get_history(limit=5000))
            except (httpx.HTTPError, ValueError) as e:
                error = str(e)
            finally:
                await plex.close()
        else:
            error = "Plex is not configured"

    last_synced = plex_cache.get_last_synced()
    cached = plex_cache.get_cached_history()

    groups: dict[str, TitleGroup] = {}
    for entry in cached:
        kind = _title_kind(entry.type)

        if bt_media_type in ("tv", "movie") and kind != bt_media_type:
            continue

        if kind == "tv":
            group_title = entry.grandparent_title or entry.title
            thumb = entry.grandparent_thumb or entry.thumb
        else:
            group_title = entry.title
            thumb = entry.thumb

        if not group_title:
            continue

        sort_title = group_title
        for prefix in ("The ", "A ", "An "):
            if sort_title.startswith(prefix):
                sort_title = sort_title[len(prefix) :]
                break
        first = sort_title[0].upper() if sort_title else "?"
        if bt_letter == "#":
            if first.isalpha():
                continue
        elif bt_letter and first != bt_letter:
            continue

        if bt_search and bt_search.lower() not in group_title.lower():
            continue

        group = groups.setdefault(
            group_title,
            TitleGroup(
                title=group_title,
                kind=kind,
                thumb=_plex_thumb_url(thumb) if thumb else None,
            ),
        )
        group.count += 1
        group.last_watched = max(group.last_watched, entry.viewed_at)
        if entry.account_id:
            group.account_ids.add(entry.account_id)

    sorted_groups = sorted(groups.values(), key=lambda g: g.title.lower())
    return templates.TemplateResponse(
        request,
        "partials/plex_bytitle.html",
        {
            "groups": sorted_groups,
            "error": error,
            "search": bt_search,
            "letter": bt_letter,
            "media_type": bt_media_type,
            "last_synced": last_synced,
            "total_cached": len(cached),
        },
    )


@router.post("/plex/bytitle/sync", response_class=HTMLResponse)
async def plex_bytitle_sync(request: Request):
    """Force-refresh the Plex history cache and return updated content."""

    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        count = plex_cache.populate_cache(await plex.get_history(limit=5000))
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success",
                "message": f"History synced — {count} items cached",
            },
            headers={"HX-Trigger": "plexBytitleSynced"},
        )
    except (httpx.HTTPError, ValueError) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": f"Sync failed: {e}"},
        )
    finally:
        await plex.close()


@router.get("/plex/butler", response_class=HTMLResponse)
async def plex_butler(request: Request):
    """HTMX partial: Butler task list with run buttons."""
    tasks = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            api_tasks = {t.name: t for t in await plex.get_butler_tasks()}
            for bt in BUTLER_TASKS:
                api = api_tasks.get(bt.name)
                tasks.append(
                    ButlerRow(
                        name=bt.name,
                        label=bt.label,
                        desc=bt.desc,
                        running=bool(api and api.running),
                        enabled=api is None or api.enabled is not False,
                    )
                )
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_butler.html",
        {"tasks": tasks, "error": error},
    )


@router.post(
    "/plex/butler/{task_name}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def run_plex_butler_task(request: Request, task_name: str):
    """Run a Plex Butler maintenance task."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.run_butler_task(task_name)
        label = next((t.label for t in BUTLER_TASKS if t.name == task_name), task_name)
        msg_type = "success" if ok else "error"
        msg = f"Started: {label}" if ok else f"Failed to start: {label}"
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": msg_type, "message": msg},
        )
    finally:
        await plex.close()


@router.delete(
    "/plex/session/{session_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def terminate_plex_session(
    request: Request,
    session_id: str,
    reason: str = Query(default="Session terminated by Arrmate"),
):
    """Terminate an active Plex streaming session (session_id = Session.id UUID)."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.terminate_session(session_id, reason)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": "Session terminated" if ok else "Failed to terminate session",
            },
            headers={"HX-Trigger": "plex-session-terminated"},
        )
    finally:
        await plex.close()


@router.post("/plex/rate", response_class=HTMLResponse)
async def rate_plex_item(
    request: Request,
    rating_key: str = Form(...),
    stars: float = Form(...),
    title: str = Form(default=""),
):
    """Rate a Plex item (1-5 stars) from the UI."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.rate_item(rating_key, stars)
        label = title or rating_key
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": (
                    f"Rated '{label}' {int(stars)} stars" if ok else f"Failed to rate '{label}'"
                ),
            },
        )
    finally:
        await plex.close()


@router.post("/plex/detect/{rating_key}/intro", response_class=HTMLResponse)
async def plex_detect_intro(request: Request, rating_key: str):
    """Trigger Plex intro detection for an item."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.detect_intro(rating_key)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": "Intro detection queued" if ok else "Failed to queue intro detection",
            },
        )
    finally:
        await plex.close()


@router.post("/plex/detect/{rating_key}/credits", response_class=HTMLResponse)
async def plex_detect_credits(request: Request, rating_key: str):
    """Trigger Plex credit detection for an item."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.detect_credits(rating_key)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": "Credit detection queued" if ok else "Failed to queue credit detection",
            },
        )
    finally:
        await plex.close()


@router.post("/plex/watched/{rating_key}", response_class=HTMLResponse)
async def plex_mark_watched(request: Request, rating_key: str):
    """Mark a Plex item as watched."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.mark_watched(rating_key)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": "Marked as watched" if ok else "Failed to mark as watched",
            },
        )
    finally:
        await plex.close()


@router.post("/plex/unwatched/{rating_key}", response_class=HTMLResponse)
async def plex_mark_unwatched(request: Request, rating_key: str):
    """Mark a Plex item as unwatched."""
    plex = _plex_client()
    if not plex:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex.mark_unwatched(rating_key)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": "Marked as unwatched" if ok else "Failed to mark as unwatched",
            },
        )
    finally:
        await plex.close()


def _duration_label(duration_ms: int) -> str:
    hours = duration_ms // 3_600_000
    minutes = (duration_ms % 3_600_000) // 60_000
    if not duration_ms:
        return ""
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


@router.get("/plex/playlists", response_class=HTMLResponse)
async def plex_playlists(request: Request):
    """HTMX partial: playlist list."""
    playlists = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            for pl in await plex.get_playlists():
                thumb_path = pl.thumb or pl.composite
                playlists.append(
                    PlaylistRow(
                        id=pl.rating_key,
                        title=pl.title or "Untitled",
                        playlist_type=pl.playlist_type or "video",
                        item_count=pl.leaf_count or 0,
                        duration=_duration_label(pl.duration or 0),
                        thumb=_plex_thumb_url(thumb_path) if thumb_path else None,
                        summary=pl.summary or "",
                    )
                )
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_playlists.html",
        {"playlists": playlists, "error": error},
    )


def _bandwidth_label(kbps: int) -> str:
    if kbps >= 1000:
        return f"{kbps // 1000} Mbps"
    return f"{kbps} Kbps" if kbps else ""


def _session_row(s: PlexMetadata) -> SessionRow:
    if s.type == "episode":
        title = s.grandparent_title or ""
        subtitle = f"{s.episode_code} — {s.title or ''}"
    else:
        title = s.title or "Unknown"
        subtitle = _year_label(s)
    user = s.user or PlexSessionUser()
    player = s.player or PlexPlayer()
    session = s.session or PlexSession()
    transcode = s.transcode_session or PlexTranscodeSession()
    source = s.media[0] if s.media else PlexMedia()
    src_video = source.video_codec or ""
    src_audio = source.audio_codec or ""
    return SessionRow(
        title=title,
        subtitle=subtitle,
        type=s.type or "",
        pct=s.progress_pct,
        user=user.title or "",
        user_thumb=user.thumb or "",
        player=player.title or "",
        platform=player.platform or "",
        state=player.state or "playing",
        location=session.location or "",
        bandwidth=_bandwidth_label(session.bandwidth or 0),
        video_decision=transcode.video_decision or "directplay",
        audio_decision=transcode.audio_decision or "directplay",
        src_video=src_video,
        src_audio=src_audio,
        src_res=f"{source.width}x{source.height or ''}" if source.width else "",
        dst_video=transcode.video_codec or src_video,
        dst_audio=transcode.audio_codec or src_audio,
        session_id=session.id or "",
        thumb=_thumb(s),
    )


@router.get("/plex/sessions", response_class=HTMLResponse)
async def plex_sessions_panel(request: Request):
    """HTMX partial: active streaming sessions with transcode/bandwidth detail."""
    sessions = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            sessions = [_session_row(s) for s in await plex.get_sessions()]
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await plex.close()
    else:
        error = "Plex is not configured"
    return templates.TemplateResponse(
        request,
        "partials/plex_sessions.html",
        {"sessions": sessions, "error": error},
    )


@router.get(
    "/plex/share",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def plex_share_panel(request: Request):
    """HTMX partial: share server management (invite + current shares)."""
    plex = _plex_client()
    plex_tv = _plex_tv_client()

    libraries = []
    friends = []
    machine_id: str | None = None
    error: str | None = None

    if not plex or not plex_tv:
        error = "Plex is not configured (PLEX_URL and PLEX_TOKEN required)"
    else:
        try:
            machine_id_result, libs_result, friends_result = await asyncio.gather(
                plex.get_machine_identifier(),
                plex.get_libraries(),
                plex_tv.get_friends(),
                return_exceptions=True,
            )
            machine_id = None if isinstance(machine_id_result, BaseException) else machine_id_result
            if not isinstance(libs_result, BaseException):
                libraries = [
                    ShareLibrary(key=int(lib.key), title=lib.title, type=lib.type)
                    for lib in libs_result
                ]
            if isinstance(friends_result, BaseException):
                error = "Could not load friends list from plex.tv"
            else:
                for friend in friends_result:
                    share = friend.share_on(machine_id)
                    if share is None:
                        continue
                    friends.append(
                        ShareFriend(
                            id=friend.id,
                            username=friend.title or friend.username or "Unknown",
                            email=friend.email or "",
                            thumb=friend.thumb or "",
                            all_libraries=share.all_libraries,
                            section_titles=[s.title or "" for s in share.sections],
                        )
                    )
        except ValueError as e:
            error = str(e)
        finally:
            await plex.close()
            await plex_tv.close()

    return templates.TemplateResponse(
        request,
        "partials/plex_share.html",
        {
            "libraries": libraries,
            "friends": friends,
            "machine_id": machine_id,
            "error": error,
        },
    )


@router.post(
    "/plex/share/invite",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def plex_share_invite(request: Request):
    """Send a Plex server share invite to an email address."""
    form = await request.form()
    email_value = form.get("email")
    email = (email_value if isinstance(email_value, str) else "").strip()
    # section_ids comes as one or more values; empty = share all
    raw_ids = form.getlist("section_ids")
    section_ids = [int(v) for v in raw_ids if isinstance(v, str) and v.isdigit()]

    if not email:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Email address is required"},
        )

    plex = _plex_client()
    plex_tv = _plex_tv_client()
    if not plex or not plex_tv:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )

    try:
        machine_id = await plex.get_machine_identifier()
        await plex.close()
        if not machine_id:
            return templates.TemplateResponse(
                request,
                "components/toast.html",
                {"type": "error", "message": "Could not get Plex server ID"},
            )
        await plex_tv.share_server(machine_id, email, section_ids)
        lib_note = (
            "all libraries"
            if not section_ids
            else f"{len(section_ids)} librar{'y' if len(section_ids) == 1 else 'ies'}"
        )
        resp = templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "success", "message": f"Invite sent to {email} ({lib_note})"},
        )
        resp.headers["HX-Trigger"] = "plexShareUpdated"
        return resp
    except httpx.HTTPError as e:
        msg = str(e)
        if "400" in msg:
            msg = "Could not send invite; user may already have access or email is unknown"
        elif "401" in msg or "403" in msg:
            msg = "Authentication failed — check your PLEX_TOKEN"
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": msg},
        )
    finally:
        await plex_tv.close()


@router.post(
    "/plex/share/remove/{friend_id}",
    response_class=HTMLResponse,
    dependencies=[Depends(require_power_user)],
)
async def plex_share_remove(request: Request, friend_id: int):
    """Revoke a friend's access to this Plex server."""
    plex_tv = _plex_tv_client()
    if not plex_tv:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Plex is not configured"},
        )
    try:
        ok = await plex_tv.remove_friend(friend_id)
        resp = templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success" if ok else "error",
                "message": "Access revoked" if ok else "Failed to revoke access",
            },
        )
        if ok:
            resp.headers["HX-Trigger"] = "plexShareUpdated"
        return resp
    finally:
        await plex_tv.close()


def _now_playing_row(s: PlexMetadata) -> NowPlayingRow:
    if s.type == "episode":
        title = f"{s.grandparent_title or ''} {s.episode_code}"
    else:
        title = s.title or "Unknown"
    player = s.player or PlexPlayer()
    return NowPlayingRow(
        title=title,
        user=(s.user or PlexSessionUser()).title or "",
        player=player.title or "",
        state=player.state or "playing",
        pct=s.progress_pct,
        type=s.type or "",
        session_key=s.session_key or "",
        # Session.id is the UUID that DELETE /status/sessions/terminate needs.
        session_id=(s.session or PlexSession()).id or "",
    )


@router.get("/plex/nowplaying", response_class=HTMLResponse)
async def plex_now_playing(request: Request):
    """HTMX partial: current Plex sessions for the navbar strip."""
    sessions = []
    plex = _plex_client()
    if plex:
        try:
            sessions = [_now_playing_row(s) for s in await plex.get_sessions()]
        except (httpx.HTTPError, ValueError):
            pass
        finally:
            await plex.close()
    return templates.TemplateResponse(
        request,
        "partials/plex_nowplaying.html",
        {"sessions": sessions},
    )
