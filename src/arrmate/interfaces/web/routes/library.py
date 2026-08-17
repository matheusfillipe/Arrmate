"""Web routes: library."""

from datetime import date, datetime, timedelta
from itertools import groupby as _groupby
from zoneinfo import ZoneInfo

import httpx as _httpx
from fastapi.responses import Response as _Response

from arrmate.core.library_service import add_first_match

from ._shared import (  # noqa: F401
    Depends,
    Form,
    HTMLResponse,
    Query,
    RadarrClient,
    Request,
    SonarrClient,
    _base_ctx,
    _format_size,
    asyncio,
    auth_router,
    get_client_for_media_type,
    httpx,
    logger,
    require_power_user,
    router,
    settings,
    sqlite3,
    templates,
)


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    media_type: str = Query(default="tv", description="Media type (tv or movie)"),
):
    """Library browser page."""
    return templates.TemplateResponse(
        request,
        "pages/library.html",
        {
            **_base_ctx(request),
            "media_type": media_type,
        },
    )


@router.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    """Search and add page."""
    return templates.TemplateResponse(
        request,
        "pages/search.html",
        {
            **_base_ctx(request),
        },
    )


@router.get("/library/items", response_class=HTMLResponse)
async def library_items(
    request: Request,
    media_type: str = Query(default="tv"),
    page: int = Query(default=1, ge=1),
):
    """Get paginated library items."""
    items = []
    has_more = False
    page_size = 50

    try:
        client = get_client_for_media_type(media_type)
        try:
            if media_type == "tv":
                raw_items = await client.get_all_items()
                for item in raw_items:
                    item_id = item.get("id")
                    poster_url = None
                    for img in item.get("images", []):
                        if img.get("coverType") == "poster":
                            poster_url = (
                                img.get("remoteUrl") or f"/web/library/poster/sonarr/{item_id}"
                            )
                            break
                    if not poster_url and item_id:
                        poster_url = f"/web/library/poster/sonarr/{item_id}"
                    stats = item.get("statistics", {})
                    items.append(
                        {
                            "id": item_id,
                            "title": item.get("title", "Unknown"),
                            "media_type": "tv",
                            "monitored": item.get("monitored", False),
                            "status": item.get("status", ""),
                            "season_count": item.get("seasonCount") or stats.get("seasonCount"),
                            "episode_count": stats.get("episodeFileCount")
                            or item.get("episodeCount"),
                            "year": item.get("year"),
                            "poster_url": poster_url,
                            "size": _format_size(stats.get("sizeOnDisk", 0)),
                            "rating": item.get("ratings", {}).get("value"),
                            "genres": item.get("genres", [])[:3],
                        }
                    )
            elif media_type == "movie":
                raw_items = await client.get_all_items()
                for item in raw_items:
                    item_id = item.get("id")
                    poster_url = None
                    for img in item.get("images", []):
                        if img.get("coverType") == "poster":
                            poster_url = (
                                img.get("remoteUrl") or f"/web/library/poster/radarr/{item_id}"
                            )
                            break
                    if not poster_url and item_id:
                        poster_url = f"/web/library/poster/radarr/{item_id}"
                    items.append(
                        {
                            "id": item_id,
                            "title": item.get("title", "Unknown"),
                            "media_type": "movie",
                            "monitored": item.get("monitored", False),
                            "status": item.get("status", ""),
                            "year": item.get("year"),
                            "size": _format_size(item.get("sizeOnDisk", 0)),
                            "poster_url": poster_url,
                            "rating": item.get("ratings", {}).get("imdb", {}).get("value")
                            or item.get("ratings", {}).get("value"),
                            "genres": item.get("genres", [])[:3],
                            "has_file": item.get("hasFile", False),
                        }
                    )
        finally:
            await client.close()

        items.sort(key=lambda x: x["title"].lower())
        start = (page - 1) * page_size
        end = start + page_size
        has_more = end < len(items)
        items = items[start:end]

    except ValueError as e:
        logger.debug(f"Service not configured for {media_type}: {e}")
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        logger.error(f"Error fetching library items: {e}")

    return templates.TemplateResponse(
        request,
        "partials/library_list.html",
        {
            "items": items,
            "media_type": media_type,
            "page": page,
            "has_more": has_more,
            **_base_ctx(request),
        },
    )


@router.get("/search/results", response_class=HTMLResponse)
async def search_results(
    request: Request,
    query: str = Query(..., min_length=1),
    media_type: str = Query(default="tv"),
):
    """Search for media and return results HTML."""
    results = []

    # Fetch library IDs/titles for cross-referencing (best-effort)
    library_tmdb_ids: set = set()
    library_titles: set = set()
    try:
        if media_type == "tv" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr_lib = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                all_series = await sonarr_lib.get_all_items()
                library_tmdb_ids = {s["tmdbId"] for s in all_series if s.get("tmdbId")}
                library_titles = {s["title"].lower() for s in all_series if s.get("title")}
            finally:
                await sonarr_lib.close()
        elif media_type == "movie" and settings.radarr_url and settings.radarr_api_key:
            radarr_lib = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                all_movies = await radarr_lib.get_all_items()
                library_tmdb_ids = {m["tmdbId"] for m in all_movies if m.get("tmdbId")}
                library_titles = {m["title"].lower() for m in all_movies if m.get("title")}
            finally:
                await radarr_lib.close()
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        pass  # library check is best-effort; don't block search results

    try:
        client = get_client_for_media_type(media_type)
        try:
            raw_results = await client.search(query)
            for item in raw_results[:20]:
                tmdb_id = item.get("tmdbId")
                result = {
                    "title": item.get("title", "Unknown"),
                    "media_type": media_type,
                    "year": item.get("year"),
                    "overview": item.get("overview", ""),
                    "status": item.get("status", ""),
                    "poster_url": None,
                    "network": item.get("network"),
                    "rating": None,
                    "quality_profiles": None,
                    "in_library": (
                        (tmdb_id in library_tmdb_ids if tmdb_id else False)
                        or item.get("title", "").lower() in library_titles
                    ),
                }

                images = item.get("images") or item.get("remotePoster")
                if isinstance(images, list):
                    for img in images:
                        if img.get("coverType") == "poster":
                            result["poster_url"] = img.get("remoteUrl") or img.get("url")
                            break
                elif isinstance(images, str):
                    result["poster_url"] = images

                ratings = item.get("ratings")
                if ratings and isinstance(ratings, dict):
                    value = ratings.get("value")
                    if value:
                        result["rating"] = f"{value:.1f}"

                results.append(result)
        finally:
            await client.close()

    except ValueError as e:
        logger.debug(f"Service not configured for {media_type}: {e}")
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        logger.error(f"Error searching: {e}")

    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {
            "results": results,
            "query": query,
            "media_type": media_type,
            **_base_ctx(request),
        },
    )


@router.get("/search/quick-results", response_class=HTMLResponse)
async def quick_search_results(
    request: Request,
    query: str = Query(..., min_length=1),
):
    """Search both TV (Sonarr) and movies (Radarr) in parallel and return combined results."""

    async def _search_service(media_type: str) -> list[dict]:
        try:
            client = get_client_for_media_type(media_type)
            try:
                raw = await client.search(query)
            finally:
                await client.close()
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            return []

        results = []
        for item in raw[:8]:
            result = {
                "title": item.get("title", "Unknown"),
                "media_type": media_type,
                "year": item.get("year"),
                "overview": item.get("overview", ""),
                "status": item.get("status", ""),
                "poster_url": None,
                "rating": None,
                "in_library": False,
                "tmdb_id": item.get("tmdbId"),
            }
            images = item.get("images") or item.get("remotePoster")
            if isinstance(images, list):
                for img in images:
                    if img.get("coverType") == "poster":
                        result["poster_url"] = img.get("remoteUrl") or img.get("url")
                        break
            elif isinstance(images, str):
                result["poster_url"] = images
            ratings = item.get("ratings")
            if ratings and isinstance(ratings, dict) and ratings.get("value"):
                result["rating"] = f"{ratings['value']:.1f}"
            results.append(result)
        return results

    tv_results, movie_results = await asyncio.gather(
        _search_service("tv"),
        _search_service("movie"),
    )

    # Cross-reference library membership
    async def _get_library_ids(media_type: str) -> tuple[set, set]:
        try:
            if media_type == "tv" and settings.sonarr_url and settings.sonarr_api_key:
                sonarr_client = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
                try:
                    items = await sonarr_client.get_all_items()
                finally:
                    await sonarr_client.close()
            elif media_type == "movie" and settings.radarr_url and settings.radarr_api_key:
                radarr_client = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
                try:
                    items = await radarr_client.get_all_items()
                finally:
                    await radarr_client.close()
            else:
                return set(), set()
            tmdb_ids = {i["tmdbId"] for i in items if i.get("tmdbId")}
            titles = {i["title"].lower() for i in items if i.get("title")}
            return tmdb_ids, titles
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            return set(), set()

    (tv_tmdb, tv_titles), (movie_tmdb, movie_titles) = await asyncio.gather(
        _get_library_ids("tv"),
        _get_library_ids("movie"),
    )

    for r in tv_results:
        r["in_library"] = (r["tmdb_id"] in tv_tmdb if r["tmdb_id"] else False) or r[
            "title"
        ].lower() in tv_titles
    for r in movie_results:
        r["in_library"] = (r["tmdb_id"] in movie_tmdb if r["tmdb_id"] else False) or r[
            "title"
        ].lower() in movie_titles

    # Interleave: pick top results from each type, prioritise by title similarity
    query_lower = query.lower()

    def _score(r: dict) -> int:
        title = r["title"].lower()
        if title == query_lower:
            return 0
        if title.startswith(query_lower):
            return 1
        if query_lower in title:
            return 2
        return 3

    tv_results.sort(key=_score)
    movie_results.sort(key=_score)

    # Build combined list: best match first, then interleave remaining
    combined = []
    tv_q, mv_q = list(tv_results), list(movie_results)
    while tv_q or mv_q:
        if tv_q:
            combined.append(tv_q.pop(0))
        if mv_q:
            combined.append(mv_q.pop(0))

    sonarr_ok = bool(settings.sonarr_url and settings.sonarr_api_key)
    radarr_ok = bool(settings.radarr_url and settings.radarr_api_key)

    return templates.TemplateResponse(
        request,
        "partials/quick_search_results.html",
        {
            "results": combined,
            "query": query,
            "sonarr_ok": sonarr_ok,
            "radarr_ok": radarr_ok,
            **_base_ctx(request),
        },
    )


@router.post("/library/add", response_class=HTMLResponse)
async def add_to_library(
    request: Request,
    title: str = Form(...),
    media_type: str = Form(...),
):
    """Add item to library and return success toast."""
    try:
        client = get_client_for_media_type(media_type)
        try:
            item = await add_first_match(client, media_type, title)
            added_title = (
                item.get("title") or item.get("artistName") or item.get("authorName") or title
            )
            return templates.TemplateResponse(
                request,
                "components/toast.html",
                {"type": "success", "message": f"Added '{added_title}' to library"},
                headers={"HX-Trigger": "library-updated"},
            )
        finally:
            await client.close()

    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": f"Failed to add '{title}': {e!s}"},
        )


@router.get("/upcoming", response_class=HTMLResponse)
async def upcoming_page(request: Request):
    """Upcoming calendar page — episodes and movies airing in the next few days."""
    return templates.TemplateResponse(
        request,
        "pages/upcoming.html",
        {**_base_ctx(request)},
    )


@router.get("/upcoming/content", response_class=HTMLResponse)
async def upcoming_content(
    request: Request,
    days: int = Query(default=7, ge=1, le=30),
):
    """HTMX partial: combined Sonarr + Radarr calendar for the next N days."""

    _eastern = ZoneInfo("America/New_York")

    def _parse_air_time(air_date_utc_str: str) -> str | None:
        """Convert an airDateUtc string to a human-readable Eastern time string."""
        if not air_date_utc_str:
            return None
        try:
            dt_utc = datetime.fromisoformat(air_date_utc_str.replace("Z", "+00:00"))
            # Sonarr uses midnight UTC as a placeholder when the air time is unknown
            if dt_utc.hour == 0 and dt_utc.minute == 0:
                return None
            dt_east = dt_utc.astimezone(_eastern)
            h = dt_east.hour % 12 or 12
            ampm = "AM" if dt_east.hour < 12 else "PM"
            tz_abbr = "EDT" if dt_east.dst() else "EST"
            return f"{h}:{dt_east.minute:02d} {ampm} {tz_abbr}"
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            return None

    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=days)).isoformat()

    events: list = []
    error = None

    # --- Sonarr ---
    if settings.sonarr_url and settings.sonarr_api_key:
        try:
            sonarr = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            eps = await sonarr.get_calendar(start_str, end_str, include_series=True)
            await sonarr.close()
            for ep in eps:
                series = ep.get("series") or {}
                air_date = ep.get("airDate") or (ep.get("airDateUtc") or "")[:10]
                if not air_date:
                    continue
                poster = None
                for img in series.get("images") or []:
                    if img.get("coverType") == "poster":
                        url = img.get("remoteUrl") or img.get("url", "")
                        poster = url if url.startswith("http") else None
                        break
                events.append(
                    {
                        "kind": "tv",
                        "date": air_date,
                        "show": series.get("title", ""),
                        "episode_label": f"S{ep.get('seasonNumber', 0):02d}"
                        f"E{ep.get('episodeNumber', 0):02d}",
                        "title": ep.get("title", ""),
                        "network": series.get("network", ""),
                        "has_file": bool(ep.get("hasFile")),
                        "monitored": bool(ep.get("monitored")),
                        "poster": poster,
                        "air_time_est": _parse_air_time(ep.get("airDateUtc", "")),
                    }
                )
        except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
            error = str(e)

    # --- Radarr ---
    if settings.radarr_url and settings.radarr_api_key:
        try:
            radarr = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            movies = await radarr.get_calendar(start_str, end_str)
            await radarr.close()
            for m in movies:
                release_date = None
                for field in ("inCinemas", "digitalRelease", "physicalRelease"):
                    val = (m.get(field) or "")[:10]
                    if val and start_str <= val <= end_str:
                        release_date = val
                        break
                if not release_date:
                    release_date = (
                        m.get("inCinemas")
                        or m.get("digitalRelease")
                        or m.get("physicalRelease")
                        or ""
                    )[:10]
                if not release_date:
                    continue
                poster = None
                for img in m.get("images") or []:
                    if img.get("coverType") == "poster":
                        url = img.get("remoteUrl") or img.get("url", "")
                        poster = url if url.startswith("http") else None
                        break
                release_type = (
                    "Cinema"
                    if (m.get("inCinemas") or "")[:10] == release_date
                    else "Digital"
                    if (m.get("digitalRelease") or "")[:10] == release_date
                    else "Physical"
                )
                events.append(
                    {
                        "kind": "movie",
                        "date": release_date,
                        "show": "",
                        "episode_label": "",
                        "title": m.get("title", ""),
                        "network": release_type,
                        "has_file": bool(m.get("hasFile")),
                        "monitored": bool(m.get("monitored")),
                        "poster": poster,
                        "year": m.get("year"),
                        "air_time_est": None,
                    }
                )
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as ex:
            if not error:
                error = str(ex)

    events.sort(key=lambda e: (e["date"], e.get("show") or e["title"]))
    grouped = [
        {"date": d, "events": list(evs)} for d, evs in _groupby(events, key=lambda e: e["date"])
    ]

    return templates.TemplateResponse(
        request,
        "partials/upcoming_content.html",
        {
            "grouped": grouped,
            "days": days,
            "error": error,
            "total": len(events),
        },
    )


@router.post("/library/monitor", response_class=HTMLResponse)
async def toggle_monitor(
    request: Request,
    item_id: int = Form(...),
    media_type: str = Form(...),
    monitored: str = Form(...),
):
    """Toggle monitoring status for a movie or TV series."""
    new_state = monitored.lower() == "true"
    label = "Monitored" if new_state else "Unmonitored"
    try:
        if media_type == "movie":
            movie_client = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                await movie_client.set_movie_monitored(item_id, new_state)
            finally:
                await movie_client.close()
        elif media_type == "tv":
            series_client = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                await series_client.set_series_monitored(item_id, new_state)
            finally:
                await series_client.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "success", "message": f"Set to {label}"},
        )
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post("/library/upgrade", response_class=HTMLResponse)
async def upgrade_item(
    request: Request,
    item_id: int = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
):
    """Trigger a quality upgrade search for a library item."""
    try:
        if media_type == "movie":
            radarr_client_2 = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                await radarr_client_2.trigger_item_search(item_id)
            finally:
                await radarr_client_2.close()
            msg = f"Triggered upgrade search for '{title}'"
        elif media_type == "tv":
            sonarr_client_2 = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                await sonarr_client_2.trigger_item_search(item_id)
            finally:
                await sonarr_client_2.close()
            msg = f"Triggered search for '{title}'"
        else:
            msg = "Unsupported media type"
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "success", "message": msg},
        )
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post("/library/moreseasons", response_class=HTMLResponse)
async def more_seasons(
    request: Request,
    item_id: int = Form(...),
    title: str = Form(...),
):
    """Monitor all seasons of a series and trigger a full search for new episodes."""
    try:
        sonarr_client_3 = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
        try:
            await sonarr_client_3.monitor_all_seasons(item_id)
            await sonarr_client_3.trigger_item_search(item_id)
        finally:
            await sonarr_client_3.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {
                "type": "success",
                "message": f"All seasons monitored and search triggered for '{title}'",
            },
        )
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.post(
    "/library/remove", response_class=HTMLResponse, dependencies=[Depends(require_power_user)]
)
async def remove_item(
    request: Request,
    item_id: int = Form(...),
    media_type: str = Form(...),
    title: str = Form(...),
):
    """Remove a movie or TV series and delete its files — power_user/admin only."""
    try:
        if media_type == "movie":
            radarr_client_3 = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                await radarr_client_3.delete_item(item_id, delete_files=True)
            finally:
                await radarr_client_3.close()
        elif media_type == "tv":
            sonarr_client_4 = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                await sonarr_client_4.delete_item(item_id, delete_files=True)
            finally:
                await sonarr_client_4.close()
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "success", "message": f"Removed '{title}' and all files"},
            headers={"HX-Trigger": "library-updated"},
        )
    except (httpx.HTTPError, KeyError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": str(e)},
        )


@router.get("/library/poster/{service}/{item_id}", response_class=HTMLResponse)
async def library_poster(service: str, item_id: int):
    """Proxy poster images from Sonarr/Radarr (keeps API key server-side)."""

    if service == "sonarr" and settings.sonarr_url and settings.sonarr_api_key:
        url = f"{settings.sonarr_url.rstrip('/')}/api/v3/mediacover/{item_id}/poster.jpg"
        headers = {"X-Api-Key": settings.sonarr_api_key}
    elif service == "radarr" and settings.radarr_url and settings.radarr_api_key:
        url = f"{settings.radarr_url.rstrip('/')}/api/v3/mediacover/{item_id}/poster.jpg"
        headers = {"X-Api-Key": settings.radarr_api_key}
    else:
        return _Response(status_code=404)

    try:
        async with _httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return _Response(content=resp.content, media_type="image/jpeg")
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        pass
    return _Response(status_code=404)
