"""Web routes: plex."""

from arrmate.cache import plex_cache

from ._shared import (  # noqa: F401
    BUTLER_TASKS,
    Depends,
    Form,
    HTMLResponse,
    PlexClient,
    Query,
    Request,
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


@router.get("/plex", response_class=HTMLResponse)
async def plex_page(request: Request):
    """Plex hub page."""
    plex = _plex_client()
    configured = plex is not None
    accounts = []
    home_users = []
    if plex:
        try:
            raw_accounts = await plex.get_accounts()
            # Normalize: Plex returns `name` on /accounts but `title` on User objects in history
            accounts = [
                {
                    "id": a.get("id"),
                    "title": (
                        a.get("title")
                        or a.get("name")
                        or ("Main User" if a.get("id") == 1 else f"User {a.get('id', '')}")
                    ),
                }
                for a in raw_accounts
                if a.get("id") not in (None, 0)  # exclude system account 0
            ]
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            pass
        finally:
            await plex.close()
    # Load home users from plex.tv for the user switcher (managed users only)
    tv = _plex_tv_client()
    if tv:
        try:
            raw_users = await tv.get_home_users()
            home_users = [
                {
                    "id": u.get("id"),
                    "title": u.get("title") or u.get("name") or f"User {u.get('id', '')}",
                    "thumb": u.get("thumb"),
                }
                for u in raw_users
                if u.get("id") is not None and not u.get("admin", False)
            ]
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            pass
        finally:
            await tv.close()

    # For regular users: find their Plex accountID so we can lock history to them.
    # Match by username — the username stored in our DB matches the Plex account name.
    current_user = get_current_user(request)
    viewer_account_id: int | None = None
    if current_user and current_user.get("role") == "user":
        username_lower = (current_user.get("username") or "").lower()
        for acct in accounts:
            if (acct.get("title") or "").lower() == username_lower:
                viewer_account_id = int(acct.get("id") or 0)
                break

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
    import httpx as _httpx
    from fastapi.responses import Response as _Response

    if not settings.plex_url or not settings.plex_token:
        return _Response(status_code=404)
    url = f"{settings.plex_url.rstrip('/')}{path}"
    try:
        async with _httpx.AsyncClient(timeout=10) as hx:
            resp = await hx.get(
                url,
                headers={
                    "X-Plex-Token": settings.plex_token,
                    "Accept": "image/jpeg,image/*",
                },
            )
            if resp.status_code == 200:
                ct = resp.headers.get("content-type", "image/jpeg")
                return _Response(content=resp.content, media_type=ct)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        pass
    return _Response(status_code=404)


@router.get("/plex/history", response_class=HTMLResponse)
async def plex_history(
    request: Request,
    account_id: int = Query(default=0),
    days: int = Query(default=7, ge=0),  # 0 = all time
):
    """HTMX partial: watch history."""
    import time as _time

    cutoff = int(_time.time()) - (days * 86400) if days > 0 else 0
    # Fetch more items when a short window is selected so we don't miss entries
    fetch_limit = 500 if days > 0 else 200

    # Regular users can only see their own history — enforce server-side.
    current_user = get_current_user(request)
    if current_user and current_user.get("role") == "user":
        account_id = 0  # will be overridden below after accounts are fetched

    items = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            # Build account_id → display name map so history rows show real usernames.
            # Account ID 1 is always the server owner; Plex may return it with no title.
            raw_accounts = await plex.get_accounts()
            account_name_map: dict[int, str] = {}
            for acct in raw_accounts:
                acct_id = acct.get("id", 0)
                acct_name = acct.get("title") or acct.get("name") or ""
                if acct_id == 1 and not acct_name:
                    acct_name = "Main User"
                if acct_name:
                    account_name_map[acct_id] = acct_name
            # Ensure account 1 always has a label
            account_name_map.setdefault(1, "Main User")

            # For regular users, lock history to their own Plex account.
            if current_user and current_user.get("role") == "user":
                username_lower = (current_user.get("username") or "").lower()
                for acct_id, acct_name in account_name_map.items():
                    if acct_name.lower() == username_lower:
                        account_id = acct_id
                        break
                # If no match found, use a sentinel that will return no results
                if account_id == 0:
                    account_id = -1

            raw = await plex.get_history(
                account_id=account_id,
                limit=fetch_limit,
                min_date=cutoff if cutoff else None,
            )
            for item in raw:
                viewed_at = item.get("viewedAt", 0)
                # Skip items with no timestamp
                if viewed_at == 0:
                    continue
                # Secondary client-side guard (server already filters but be safe)
                if cutoff and viewed_at < cutoff:
                    continue
                media_type = item.get("type", "")
                if media_type == "episode":
                    show = item.get("grandparentTitle") or item.get("title") or ""
                    ep_title = item.get("title", "")
                    title = f"{show} — {ep_title}" if show else ep_title
                    subtitle = f"S{item.get('parentIndex', 0):02d}E{item.get('index', 0):02d}"
                else:
                    title = item.get("title") or ""
                    subtitle = str(item.get("year", "")) if item.get("year") else ""
                # Skip entries with no usable title (e.g. media removed from library)
                if not title:
                    continue
                thumb = _plex_thumb_url(item.get("thumb", "")) if item.get("thumb") else None
                # History items expose accountID as a plain int field, not a nested dict.
                item_account_id = item.get("accountID") or 0
                user_name = account_name_map.get(item_account_id, "")
                items.append(
                    {
                        "title": title,
                        "subtitle": subtitle,
                        "type": media_type,
                        "viewed_at": viewed_at,
                        "thumb": thumb,
                        "rating_key": item.get("ratingKey"),
                        "user": user_name,
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        logger.warning("failed to list Plex accounts for account match", exc_info=True)
        return 0
    finally:
        await plex.close()
    target = (username or "").lower()
    for acct in accounts:
        name = acct.get("title") or acct.get("name") or ""
        if name and name.lower() == target:
            return int(acct.get("id", 0))
    return 0


async def _plex_client_scoped(
    request: Request, user_id: int
) -> tuple[PlexClient | None, str | None]:
    """Plex client for a household view; user role is locked to their own account."""
    current_user = get_current_user(request)
    if current_user and current_user.get("role") == "user":
        matched = await _plex_account_id_for_username(current_user.get("username") or "")
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
            raw = await plex.get_continue_watching()
            for item in raw:
                duration = item.get("duration", 0)
                offset = item.get("viewOffset", 0)
                pct = int(offset / duration * 100) if duration else 0
                media_type = item.get("type", "")
                if media_type == "episode":
                    title = item.get("grandparentTitle", item.get("title", "Unknown"))
                    subtitle = (
                        f"S{item.get('parentIndex', 0):02d}E{item.get('index', 0):02d} - "
                        f"{item.get('title', '')}"
                    )
                else:
                    title = item.get("title", "Unknown")
                    subtitle = str(item.get("year", "")) if item.get("year") else ""
                thumb = item.get("thumb") or item.get("grandparentThumb")
                items.append(
                    {
                        "title": title,
                        "subtitle": subtitle,
                        "type": media_type,
                        "pct": pct,
                        "thumb": _plex_thumb_url(thumb) if thumb else None,
                        "rating_key": item.get("ratingKey"),
                        "year": item.get("year"),
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
            raw = await plex.get_on_deck()
            for item in raw:
                media_type = item.get("type", "")
                if media_type == "episode":
                    title = item.get("grandparentTitle", item.get("title", "Unknown"))
                    subtitle = (
                        f"S{item.get('parentIndex', 0):02d}E{item.get('index', 0):02d} - "
                        f"{item.get('title', '')}"
                    )
                else:
                    title = item.get("title", "Unknown")
                    subtitle = ""
                thumb = item.get("thumb") or item.get("grandparentThumb")
                items.append(
                    {
                        "title": title,
                        "subtitle": subtitle,
                        "type": media_type,
                        "thumb": _plex_thumb_url(thumb) if thumb else None,
                        "rating_key": item.get("ratingKey"),
                        "year": item.get("year"),
                        "summary": item.get("summary", "")[:120],
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
            raw = await plex.get_recently_added(limit=limit)
            for item in raw:
                media_type = item.get("type", "")
                if media_type == "episode":
                    title = item.get("grandparentTitle", item.get("title", "Unknown"))
                    subtitle = (
                        f"S{item.get('parentIndex', 0):02d}E{item.get('index', 0):02d} - "
                        f"{item.get('title', '')}"
                    )
                elif media_type == "season":
                    title = item.get("parentTitle", item.get("title", "Unknown"))
                    subtitle = item.get("title", "")
                else:
                    title = item.get("title", "Unknown")
                    subtitle = str(item.get("year", "")) if item.get("year") else ""
                thumb = item.get("thumb") or item.get("grandparentThumb")
                items.append(
                    {
                        "title": title,
                        "subtitle": subtitle,
                        "type": media_type,
                        "thumb": _plex_thumb_url(thumb) if thumb else None,
                        "rating_key": item.get("ratingKey"),
                        "year": item.get("year"),
                        "added_at": item.get("addedAt", 0),
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
    last_synced = None

    # Seed the cache on first load (if empty or stale)
    if plex_cache.is_stale():
        plex = _plex_client()
        if plex:
            try:
                raw = await plex.get_history(limit=5000)
                plex_cache.populate_cache(raw)
            except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
                error = str(e)
            finally:
                await plex.close()
        else:
            error = "Plex is not configured"

    last_synced = plex_cache.get_last_synced()
    cached = plex_cache.get_cached_history()

    groups: dict = {}
    for item in cached:
        media_type_str = item.get("type", "")
        kind = "tv" if media_type_str in ("episode", "season") else "movie"

        if bt_media_type == "tv" and kind != "tv":
            continue
        if bt_media_type == "movie" and kind != "movie":
            continue

        if kind == "tv":
            group_title = item.get("grandparent_title") or item.get("title") or ""
            thumb = item.get("grandparent_thumb") or item.get("thumb")
        else:
            group_title = item.get("title") or ""
            thumb = item.get("thumb")

        if not group_title:
            continue

        # Letter filter
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

        if group_title not in groups:
            groups[group_title] = {
                "title": group_title,
                "kind": kind,
                "thumb": _plex_thumb_url(thumb) if thumb else None,
                "count": 0,
                "last_watched": 0,
                "unique_accounts": set(),
            }
        groups[group_title]["count"] += 1
        viewed_at = item.get("viewed_at") or 0
        if viewed_at > groups[group_title]["last_watched"]:
            groups[group_title]["last_watched"] = viewed_at
        acct = item.get("account_id")
        if acct:
            groups[group_title]["unique_accounts"].add(acct)

    # Convert sets to counts for template
    for g in groups.values():
        g["user_count"] = len(g.pop("unique_accounts"))

    sorted_groups = sorted(groups.values(), key=lambda g: g["title"].lower())
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
        raw = await plex.get_history(limit=5000)
        count = plex_cache.populate_cache(raw)
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success",
                "message": f"History synced — {count} items cached",
            },
            headers={"HX-Trigger": "plexBytitleSynced"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
            api_tasks = await plex.get_butler_tasks()
            api_map = {t.get("name"): t for t in api_tasks}
            for bt in BUTLER_TASKS:
                api = api_map.get(bt["name"], {})
                tasks.append(
                    {
                        "name": bt["name"],
                        "label": bt["label"],
                        "desc": bt["desc"],
                        "running": api.get("running", False),
                        "enabled": api.get("enabled", True),
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
        label = next((t["label"] for t in BUTLER_TASKS if t["name"] == task_name), task_name)
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


@router.get("/plex/playlists", response_class=HTMLResponse)
async def plex_playlists(request: Request):
    """HTMX partial: playlist list."""
    playlists = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            raw = await plex.get_playlists()
            for pl in raw:
                duration_ms = pl.get("duration", 0) or 0
                duration_h = duration_ms // 3_600_000
                duration_m = (duration_ms % 3_600_000) // 60_000
                duration_str = (
                    (f"{duration_h}h {duration_m}m" if duration_h else f"{duration_m}m")
                    if duration_ms
                    else ""
                )
                thumb_path = pl.get("thumb") or pl.get("composite")
                playlists.append(
                    {
                        "id": pl.get("ratingKey"),
                        "title": pl.get("title", "Untitled"),
                        "playlist_type": pl.get("playlistType", "video"),
                        "item_count": pl.get("leafCount", 0),
                        "duration": duration_str,
                        "thumb": _plex_thumb_url(thumb_path) if thumb_path else None,
                        "summary": pl.get("summary", ""),
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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


@router.get("/plex/sessions", response_class=HTMLResponse)
async def plex_sessions_panel(request: Request):
    """HTMX partial: active streaming sessions with transcode/bandwidth detail."""
    sessions = []
    error = None
    plex = _plex_client()
    if plex:
        try:
            raw = await plex.get_sessions()
            for s in raw:
                media_type = s.get("type", "")
                if media_type == "episode":
                    title = s.get("grandparentTitle", "")
                    subtitle = (
                        f"S{s.get('parentIndex', 0):02d}E{s.get('index', 0):02d}"
                        f" — {s.get('title', '')}"
                    )
                else:
                    title = s.get("title", "Unknown")
                    subtitle = str(s.get("year", "")) if s.get("year") else ""
                duration = s.get("duration", 0)
                offset = s.get("viewOffset", 0)
                pct = int(offset / duration * 100) if duration else 0
                # Transcode info
                tc = s.get("TranscodeSession") or {}
                video_decision = tc.get("videoDecision", "directplay")
                audio_decision = tc.get("audioDecision", "directplay")
                # Source codec
                media_list = s.get("Media") or [{}]
                src = media_list[0] if media_list else {}
                src_video = src.get("videoCodec", "")
                src_audio = src.get("audioCodec", "")
                src_res = (
                    f"{src.get('width', '')}x{src.get('height', '')}" if src.get("width") else ""
                )
                # Bandwidth
                bandwidth = s.get("Session", {}).get("bandwidth", 0) or 0
                bw_str = (
                    f"{bandwidth // 1000} Mbps"
                    if bandwidth >= 1000
                    else (f"{bandwidth} Kbps" if bandwidth else "")
                )
                sessions.append(
                    {
                        "title": title,
                        "subtitle": subtitle,
                        "type": media_type,
                        "pct": pct,
                        "user": s.get("User", {}).get("title", ""),
                        "user_thumb": s.get("User", {}).get("thumb", ""),
                        "player": s.get("Player", {}).get("title", ""),
                        "platform": s.get("Player", {}).get("platform", ""),
                        "state": s.get("Player", {}).get("state", "playing"),
                        "location": s.get("Session", {}).get("location", ""),
                        "bandwidth": bw_str,
                        "video_decision": video_decision,
                        "audio_decision": audio_decision,
                        "src_video": src_video,
                        "src_audio": src_audio,
                        "src_res": src_res,
                        "dst_video": tc.get("videoCodec", src_video),
                        "dst_audio": tc.get("audioCodec", src_audio),
                        "session_id": s.get("Session", {}).get("id", ""),
                        "thumb": _plex_thumb_url(s.get("thumb") or s.get("grandparentThumb", ""))
                        if (s.get("thumb") or s.get("grandparentThumb"))
                        else None,
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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

    libraries: list = []
    friends: list = []
    machine_id: str | None = None
    error: str | None = None

    if not plex or not plex_tv:
        error = "Plex is not configured (PLEX_URL and PLEX_TOKEN required)"
    else:
        try:
            machine_id, raw_libs, raw_friends = await asyncio.gather(
                plex.get_machine_identifier(),
                plex.get_libraries(),
                plex_tv.get_friends(),
                return_exceptions=True,
            )
            if isinstance(machine_id, Exception):
                machine_id = None
            if isinstance(raw_libs, Exception):
                raw_libs = []
            if isinstance(raw_friends, Exception):
                raw_friends = []
                error = "Could not load friends list from plex.tv"

            # Build clean library list (key as int for matching)
            for lib in raw_libs or []:
                libraries.append(
                    {
                        "key": int(lib.get("key", 0)),
                        "title": lib.get("title", ""),
                        "type": lib.get("type", ""),
                    }
                )

            # Filter friends to those who have access to THIS server
            for f in raw_friends or []:
                servers = f.get("servers") or []
                on_this_server = any(s.get("machineIdentifier") == machine_id for s in servers)
                if on_this_server:
                    server_info: dict = next(
                        (s for s in servers if s.get("machineIdentifier") == machine_id),
                        {},
                    )
                    shared_sections = server_info.get("sections") or []
                    friends.append(
                        {
                            "id": f.get("id"),
                            "username": f.get("title") or f.get("username") or "Unknown",
                            "email": f.get("email", ""),
                            "thumb": f.get("thumb", ""),
                            "all_libraries": server_info.get("allLibraries", False),
                            "section_titles": [s.get("title", "") for s in shared_sections],
                        }
                    )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
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


@router.get("/plex/nowplaying", response_class=HTMLResponse)
async def plex_now_playing(request: Request):
    """HTMX partial: current Plex sessions for the navbar strip."""
    sessions = []
    if settings.plex_url and settings.plex_token:
        client = PlexClient(settings.plex_url, settings.plex_token)
        try:
            raw = await client.get_sessions()
            for s in raw:
                media_type = s.get("type", "")
                if media_type == "episode":
                    title = (
                        f"{s.get('grandparentTitle', '')} "
                        f"S{s.get('parentIndex', 0):02d}E{s.get('index', 0):02d}"
                    )
                elif media_type == "movie":
                    title = s.get("title", "Unknown")
                else:
                    title = s.get("title", "Unknown")
                duration = s.get("duration", 0)
                offset = s.get("viewOffset", 0)
                pct = int(offset / duration * 100) if duration else 0
                sessions.append(
                    {
                        "title": title,
                        "user": s.get("User", {}).get("title", ""),
                        "player": s.get("Player", {}).get("title", ""),
                        "state": s.get("Player", {}).get("state", "playing"),
                        "pct": pct,
                        "type": media_type,
                        "session_key": s.get("sessionKey", ""),
                        # Session.id is the UUID required by DELETE /status/sessions/terminate
                        "session_id": s.get("Session", {}).get("id", ""),
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            pass
        finally:
            await client.close()
    return templates.TemplateResponse(
        request,
        "partials/plex_nowplaying.html",
        {"sessions": sessions},
    )
