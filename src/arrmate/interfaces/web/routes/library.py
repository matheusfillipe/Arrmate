"""Web routes: library."""

from datetime import date, datetime, timedelta
from itertools import groupby as _groupby
from typing import Literal
from zoneinfo import ZoneInfo

import httpx as _httpx
from fastapi.responses import Response as _Response
from pydantic import BaseModel

from arrmate.clients.base_arr import remote_poster
from arrmate.clients.radarr import Movie, MovieLookup, MovieRatings
from arrmate.clients.sonarr import Episode, Series, SeriesLookup
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

MediaKind = Literal["tv", "movie"]


class LibraryCard(BaseModel):
    id: int
    title: str
    media_type: MediaKind
    monitored: bool
    status: str
    year: int
    poster_url: str
    size: str
    genres: list[str]
    rating: float | None = None
    season_count: int | None = None
    episode_count: int | None = None
    has_file: bool | None = None


class SearchCard(BaseModel):
    title: str
    media_type: MediaKind
    year: int
    status: str
    in_library: bool
    overview: str | None = None
    poster_url: str | None = None
    network: str | None = None
    rating: str | None = None
    tmdb_id: int | None = None


class UpcomingEvent(BaseModel):
    kind: MediaKind
    date: str
    show: str
    episode_label: str
    title: str
    network: str
    has_file: bool
    monitored: bool
    poster: str | None = None
    year: int | None = None
    air_time_est: str | None = None


class UpcomingDay(BaseModel):
    date: str
    events: list[UpcomingEvent]


class LibraryKeys(BaseModel):
    """What identifies a library item when matching lookup results against it."""

    tmdb_ids: set[int] = set()
    titles: set[str] = set()

    def has(self, card: SearchCard) -> bool:
        return (card.tmdb_id in self.tmdb_ids if card.tmdb_id else False) or (
            card.title.lower() in self.titles
        )


def _movie_rating(ratings: MovieRatings | None) -> float | None:
    if ratings is None:
        return None
    rating = ratings.imdb or ratings.tmdb
    return rating.value if rating else None


def _series_card(series: Series) -> LibraryCard:
    stats = series.statistics
    return LibraryCard(
        id=series.id,
        title=series.title,
        media_type="tv",
        monitored=series.monitored,
        status=series.status,
        year=series.year,
        poster_url=remote_poster(series.images) or f"/web/library/poster/sonarr/{series.id}",
        size=_format_size(stats.size_on_disk if stats else 0),
        genres=series.genres[:3],
        rating=series.ratings.value if series.ratings else None,
        season_count=stats.season_count if stats else None,
        episode_count=stats.episode_file_count if stats else None,
    )


def _movie_card(movie: Movie) -> LibraryCard:
    return LibraryCard(
        id=movie.id,
        title=movie.title,
        media_type="movie",
        monitored=movie.monitored,
        status=movie.status,
        year=movie.year,
        poster_url=remote_poster(movie.images) or f"/web/library/poster/radarr/{movie.id}",
        size=_format_size(movie.size_on_disk or 0),
        genres=movie.genres[:3],
        rating=_movie_rating(movie.ratings),
        has_file=movie.has_file or False,
    )


def _search_card(lookup: SeriesLookup | MovieLookup, keys: LibraryKeys) -> SearchCard:
    match lookup:
        case SeriesLookup():
            rating = lookup.ratings.value if lookup.ratings else None
            card = SearchCard(
                title=lookup.title,
                media_type="tv",
                year=lookup.year,
                status=lookup.status,
                in_library=False,
                overview=lookup.overview,
                poster_url=remote_poster(lookup.images) or lookup.remote_poster,
                network=lookup.network,
                rating=f"{rating:.1f}" if rating else None,
                tmdb_id=lookup.tmdb_id or None,
            )
        case MovieLookup():
            movie_rating = _movie_rating(lookup.ratings)
            card = SearchCard(
                title=lookup.title,
                media_type="movie",
                year=lookup.year,
                status=lookup.status,
                in_library=False,
                overview=lookup.overview,
                poster_url=remote_poster(lookup.images) or lookup.remote_poster,
                rating=f"{movie_rating:.1f}" if movie_rating else None,
                tmdb_id=lookup.tmdb_id,
            )
    card.in_library = keys.has(card)
    return card


async def _library_keys(media_type: str) -> LibraryKeys:
    """TMDB ids and titles already in the library; empty when the service is unreachable."""
    try:
        if media_type == "tv" and settings.sonarr_url and settings.sonarr_api_key:
            sonarr = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
            try:
                series = await sonarr.get_all_items()
            finally:
                await sonarr.close()
            return LibraryKeys(
                tmdb_ids={s.tmdb_id for s in series if s.tmdb_id},
                titles={s.title.lower() for s in series},
            )
        if media_type == "movie" and settings.radarr_url and settings.radarr_api_key:
            radarr = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
            try:
                movies = await radarr.get_all_items()
            finally:
                await radarr.close()
            return LibraryKeys(
                tmdb_ids={m.tmdb_id for m in movies},
                titles={m.title.lower() for m in movies},
            )
    except (httpx.HTTPError, ValueError):
        logger.debug("library lookup for %s failed", media_type, exc_info=True)
    return LibraryKeys()


async def _search_cards(media_type: str, query: str, limit: int) -> list[SearchCard]:
    """Lookup matches for a query, marked with whether each is already in the library."""
    keys = await _library_keys(media_type)
    client = get_client_for_media_type(media_type)
    try:
        match client:
            case SonarrClient() | RadarrClient():
                lookups = (await client.search(query))[:limit]
            case _:
                return []
    finally:
        await client.close()
    return [_search_card(lookup, keys) for lookup in lookups]


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
    items: list[LibraryCard] = []
    has_more = False
    page_size = 50

    try:
        client = get_client_for_media_type(media_type)
        try:
            match client:
                case SonarrClient():
                    items = [_series_card(series) for series in await client.get_all_items()]
                case RadarrClient():
                    items = [_movie_card(movie) for movie in await client.get_all_items()]
        finally:
            await client.close()

        items.sort(key=lambda card: card.title.lower())
        start = (page - 1) * page_size
        end = start + page_size
        has_more = end < len(items)
        items = items[start:end]

    except ValueError as e:
        logger.debug(f"Service not configured for {media_type}: {e}")
    except (httpx.HTTPError, sqlite3.Error) as e:
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
    results: list[SearchCard] = []
    try:
        results = await _search_cards(media_type, query, limit=20)
    except ValueError as e:
        logger.debug(f"Service not configured for {media_type}: {e}")
    except (httpx.HTTPError, sqlite3.Error) as e:
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

    async def _search_service(media_type: str) -> list[SearchCard]:
        try:
            return await _search_cards(media_type, query, limit=8)
        except (httpx.HTTPError, ValueError, sqlite3.Error):
            return []

    tv_results, movie_results = await asyncio.gather(
        _search_service("tv"),
        _search_service("movie"),
    )

    # Interleave: pick top results from each type, prioritise by title similarity
    query_lower = query.lower()

    def _score(card: SearchCard) -> int:
        title = card.title.lower()
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
    combined: list[SearchCard] = []
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
            added = await add_first_match(client, title)
            return templates.TemplateResponse(
                request,
                "components/toast.html",
                {"type": "success", "message": f"Added '{added.title}' to library"},
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


_EASTERN = ZoneInfo("America/New_York")


def _parse_air_time(air_date_utc: str | None) -> str | None:
    """Convert an airDateUtc string to a human-readable Eastern time string."""
    if not air_date_utc:
        return None
    try:
        dt_utc = datetime.fromisoformat(air_date_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    # Sonarr uses midnight UTC as a placeholder when the air time is unknown
    if dt_utc.hour == 0 and dt_utc.minute == 0:
        return None
    dt_east = dt_utc.astimezone(_EASTERN)
    h = dt_east.hour % 12 or 12
    ampm = "AM" if dt_east.hour < 12 else "PM"
    tz_abbr = "EDT" if dt_east.dst() else "EST"
    return f"{h}:{dt_east.minute:02d} {ampm} {tz_abbr}"


def _episode_event(episode: Episode) -> UpcomingEvent | None:
    air_date = episode.air_date or (episode.air_date_utc or "")[:10]
    if not air_date:
        return None
    series = episode.series
    return UpcomingEvent(
        kind="tv",
        date=air_date,
        show=series.title if series else "",
        episode_label=episode.label,
        title=episode.title,
        network=(series.network or "") if series else "",
        has_file=episode.has_file,
        monitored=episode.monitored,
        poster=remote_poster(series.images) if series else None,
        air_time_est=_parse_air_time(episode.air_date_utc),
    )


def _movie_event(movie: Movie, start: str, end: str) -> UpcomingEvent | None:
    releases = [
        ("Cinema", (movie.in_cinemas or "")[:10]),
        ("Digital", (movie.digital_release or "")[:10]),
        ("Physical", (movie.physical_release or "")[:10]),
    ]
    in_window = [(kind, day) for kind, day in releases if day and start <= day <= end]
    known = [(kind, day) for kind, day in releases if day]
    if not known:
        return None
    release_type, release_date = (in_window or known)[0]
    return UpcomingEvent(
        kind="movie",
        date=release_date,
        show="",
        episode_label="",
        title=movie.title,
        network=release_type,
        has_file=bool(movie.has_file),
        monitored=movie.monitored,
        poster=remote_poster(movie.images),
        year=movie.year,
    )


@router.get("/upcoming/content", response_class=HTMLResponse)
async def upcoming_content(
    request: Request,
    days: int = Query(default=7, ge=1, le=30),
):
    """HTMX partial: combined Sonarr + Radarr calendar for the next N days."""
    today = date.today()
    start_str = today.isoformat()
    end_str = (today + timedelta(days=days)).isoformat()

    events: list[UpcomingEvent] = []
    error = None

    if settings.sonarr_url and settings.sonarr_api_key:
        sonarr = SonarrClient(str(settings.sonarr_url), str(settings.sonarr_api_key))
        try:
            for episode in await sonarr.get_calendar(start_str, end_str):
                event = _episode_event(episode)
                if event:
                    events.append(event)
        except (httpx.HTTPError, ValueError) as e:
            error = str(e)
        finally:
            await sonarr.close()

    if settings.radarr_url and settings.radarr_api_key:
        radarr = RadarrClient(str(settings.radarr_url), str(settings.radarr_api_key))
        try:
            for movie in await radarr.get_calendar(start_str, end_str):
                movie_event = _movie_event(movie, start_str, end_str)
                if movie_event:
                    events.append(movie_event)
        except (httpx.HTTPError, ValueError) as ex:
            if not error:
                error = str(ex)
        finally:
            await radarr.close()

    events.sort(key=lambda e: (e.date, e.show or e.title))
    grouped = [
        UpcomingDay(date=day, events=list(day_events))
        for day, day_events in _groupby(events, key=lambda e: e.date)
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
