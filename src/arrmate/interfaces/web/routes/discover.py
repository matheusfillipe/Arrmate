"""Web routes: discover."""

from ._shared import (  # noqa: F401
    _AUDIOBOOK_CATEGORIES,
    _BOOK_CATEGORIES,
    _DISCOVER_CATEGORIES,
    _MUSIC_CATEGORIES,
    Depends,
    Form,
    HTMLResponse,
    LidarrClient,
    OpenLibraryClient,
    Query,
    RadarrClient,
    ReadMeABookClient,
    Request,
    SonarrClient,
    _base_ctx,
    _lastfm_client,
    _tmdb_client,
    auth_router,
    httpx,
    logger,
    require_power_user,
    router,
    settings,
    sqlite3,
    templates,
)


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(request: Request):
    return templates.TemplateResponse(
        request,
        "pages/discover.html",
        {
            "tmdb_ok": bool(settings.tmdb_api_key),
            "lastfm_ok": bool(settings.lastfm_api_key),
            "sonarr_ok": bool(settings.sonarr_url and settings.sonarr_api_key),
            "radarr_ok": bool(settings.radarr_url and settings.radarr_api_key),
            "lidarr_ok": bool(settings.lidarr_url and settings.lidarr_api_key),
            "readmeabook_ok": bool(settings.readmeabook_url and settings.readmeabook_api_key),
            "readarr_ok": bool(settings.readarr_url and settings.readarr_api_key),
            # Open Library needs no key — always available
            "openlibrary_ok": True,
            **_base_ctx(request),
        },
    )


@router.get("/discover/results", response_class=HTMLResponse)
async def discover_results(
    request: Request,
    category: str = Query(default="trending_movies"),
):
    media_type = _DISCOVER_CATEGORIES.get(category, ("", "movie"))[1]
    items: list = []
    source = "tmdb"
    error = None

    try:
        # ── Music (Last.fm) ───────────────────────────────────────────────────
        if category in _MUSIC_CATEGORIES:
            source = "lastfm"
            lfm = _lastfm_client()
            if not lfm:
                error = "LASTFM_API_KEY is not configured."
            else:
                try:
                    if category == "top_artists":
                        items = await lfm.get_top_artists()
                    else:
                        items = await lfm.get_top_tracks()
                finally:
                    await lfm.close()

            # Cross-reference Lidarr library by artist name
            library_names: set[str] = set()
            if settings.lidarr_url and settings.lidarr_api_key:
                try:
                    lidarr = LidarrClient(settings.lidarr_url, settings.lidarr_api_key)
                    all_artists = await lidarr.get_all_items()
                    await lidarr.close()
                    library_names = {
                        a.get("artistName", "").lower() for a in all_artists if a.get("artistName")
                    }
                except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                    pass
            for item in items:
                item["in_library"] = item.get("display_title", "").lower() in library_names

        # ── Books (Open Library) ──────────────────────────────────────────────
        elif category in _BOOK_CATEGORIES:
            source = "openlibrary"
            ol = OpenLibraryClient()
            try:
                if category == "books_trending":
                    items = await ol.get_trending_daily()
                elif category == "books_weekly":
                    items = await ol.get_trending_weekly()
                elif category == "books_fiction":
                    items = await ol.get_subject("fiction")
                elif category == "books_mystery":
                    items = await ol.get_subject("mystery")
                elif category == "books_scifi":
                    items = await ol.get_subject("science_fiction")
            finally:
                await ol.close()

            # Cross-reference Readarr library by title
            library_names = set()
            if settings.readarr_url and settings.readarr_api_key:
                try:
                    from arrmate.clients.readarr import ReadarrClient

                    readarr = ReadarrClient(settings.readarr_url, settings.readarr_api_key)
                    all_authors = await readarr.get_all_items()
                    await readarr.close()
                    library_names = {
                        a.get("authorName", "").lower() for a in all_authors if a.get("authorName")
                    }
                except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                    pass
            for item in items:
                item["in_library"] = item.get("author", "").lower() in library_names

        # ── Audiobooks (ReadMeABook) ───────────────────────────────────────────
        elif category in _AUDIOBOOK_CATEGORIES:
            source = "readmeabook"
            if not (settings.readmeabook_url and settings.readmeabook_api_key):
                error = "ReadMeABook is not configured."
            else:
                rmab = ReadMeABookClient(settings.readmeabook_url, settings.readmeabook_api_key)
                try:
                    if category == "audiobooks_popular":
                        raw = await rmab.get_popular()
                    else:
                        raw = await rmab.get_new_releases()

                    # Normalise to common card schema
                    existing_asins: set[str] = set()
                    try:
                        reqs = await rmab.get_requests()
                        existing_asins = {r.get("asin", "") for r in reqs if r.get("asin")}
                    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                        pass

                    for b in raw:
                        title = b.get("title") or b.get("name", "Unknown")
                        asin = b.get("asin", "")
                        items.append(
                            {
                                "display_title": title,
                                "author": b.get("author", ""),
                                "year": "",
                                "poster": b.get("image") or b.get("cover") or b.get("coverUrl"),
                                "overview": b.get("description", ""),
                                "asin": asin,
                                "media_type": "audiobook",
                                "in_library": asin in existing_asins,
                            }
                        )
                finally:
                    await rmab.close()

        # ── Movies / TV (TMDB) ────────────────────────────────────────────────
        else:
            source = "tmdb"
            tmdb = _tmdb_client()
            if not tmdb:
                error = "TMDB_API_KEY is not configured."
            else:
                try:
                    if category == "trending_movies":
                        items = await tmdb.get_trending_movies()
                    elif category == "trending_tv":
                        items = await tmdb.get_trending_tv()
                    elif category == "upcoming":
                        items = await tmdb.get_upcoming_movies()
                    elif category == "now_playing":
                        items = await tmdb.get_now_playing()
                    elif category == "on_the_air":
                        items = await tmdb.get_tv_on_the_air()
                    elif category == "popular_movies":
                        items = await tmdb.get_popular_movies()
                    elif category == "popular_tv":
                        items = await tmdb.get_popular_tv()
                    elif category == "top_rated_movies":
                        items = await tmdb.get_top_rated_movies()
                    elif category == "top_rated_tv":
                        items = await tmdb.get_top_rated_tv()
                    else:
                        items = await tmdb.get_trending_movies()

                    for item in items:
                        item["poster"] = tmdb.poster_url(item.get("poster_path"), "w342")
                        raw_date = item.get("release_date") or item.get("first_air_date") or ""
                        item["year"] = raw_date[:4] if raw_date else ""
                        item["display_title"] = item.get("title") or item.get("name") or "Unknown"
                        item["rating"] = round(item.get("vote_average", 0), 1)
                        item["media_type"] = media_type

                    # Cross-reference library
                    library_tmdb_ids: set[int] = set()
                    library_titles: set[str] = set()
                    if media_type == "movie" and settings.radarr_url and settings.radarr_api_key:
                        try:
                            radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
                            all_movies = await radarr.get_all_items()
                            await radarr.close()
                            library_tmdb_ids = {m["tmdbId"] for m in all_movies if m.get("tmdbId")}
                        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                            pass
                    elif media_type == "tv" and settings.sonarr_url and settings.sonarr_api_key:
                        try:
                            sonarr_lib = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
                            all_series = await sonarr_lib.get_all_items()
                            await sonarr_lib.close()
                            library_tmdb_ids = {s["tmdbId"] for s in all_series if s.get("tmdbId")}
                            library_titles = {
                                s["title"].lower() for s in all_series if s.get("title")
                            }
                        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                            pass
                    for item in items:
                        by_id = item.get("id") in library_tmdb_ids
                        by_title = item.get("display_title", "").lower() in library_titles
                        item["in_library"] = by_id or by_title
                finally:
                    await tmdb.close()

    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        logger.error("Discover error (category=%s): %s", category, e)
        error = str(e)
        items = []

    return templates.TemplateResponse(
        request,
        "partials/discover_results.html",
        {
            "items": items,
            "media_type": media_type,
            "source": source,
            "error": error,
            "sonarr_ok": bool(settings.sonarr_url and settings.sonarr_api_key),
            "radarr_ok": bool(settings.radarr_url and settings.radarr_api_key),
            "lidarr_ok": bool(settings.lidarr_url and settings.lidarr_api_key),
            "readmeabook_ok": bool(settings.readmeabook_url and settings.readmeabook_api_key),
            "readarr_ok": bool(settings.readarr_url and settings.readarr_api_key),
            **_base_ctx(request),
        },
    )


@router.post("/discover/add", response_class=HTMLResponse)
async def discover_add(
    request: Request,
    media_type: str = Form(...),
    tmdb_id: int = Form(...),
    title: str = Form(...),
    _: None = Depends(require_power_user),
):
    """Add a discovered title to Radarr (movies) or Sonarr (TV)."""
    try:
        if media_type == "movie":
            if not (settings.radarr_url and settings.radarr_api_key):
                raise ValueError("Radarr is not configured")
            radarr = RadarrClient(settings.radarr_url, settings.radarr_api_key)
            profiles = await radarr.get_quality_profiles()
            root_folders = await radarr.get_root_folders()
            if not profiles or not root_folders:
                raise ValueError("Radarr has no quality profiles or root folders configured")
            added = await radarr.add_movie(
                tmdb_id=tmdb_id,
                title=title,
                quality_profile_id=profiles[0]["id"],
                root_folder_path=root_folders[0]["path"],
            )
            await radarr.close()
            msg = f"Added '{added.get('title', title)}' to Radarr"
            success = True

        elif media_type == "tv":
            if not (settings.sonarr_url and settings.sonarr_api_key):
                raise ValueError("Sonarr is not configured")
            # Get TVDB ID from TMDB
            tmdb = _tmdb_client()
            if not tmdb:
                raise ValueError("TMDB API key not configured")
            ext = await tmdb.get_external_ids(tmdb_id, "tv")
            await tmdb.close()
            tvdb_id = ext.get("tvdb_id")
            if not tvdb_id:
                raise ValueError(
                    f"Could not find TVDB ID for '{title}' — it may not be in TVDB yet"
                )

            sonarr = SonarrClient(settings.sonarr_url, settings.sonarr_api_key)
            profiles = await sonarr.get_quality_profiles()
            root_folders = await sonarr.get_root_folders()
            if not profiles or not root_folders:
                raise ValueError("Sonarr has no quality profiles or root folders configured")
            # Lookup via tvdb: to get the full series object (titleSlug, seasons, etc.)
            lookup = await sonarr.search(f"tvdb:{tvdb_id}")
            if not lookup:
                raise ValueError(f"Could not find '{title}' in Sonarr's database (TVDB:{tvdb_id})")
            # Pass the full lookup result so all Sonarr-required fields are present
            added = await sonarr.add_series_from_lookup(
                lookup_result=lookup[0],
                quality_profile_id=profiles[0]["id"],
                root_folder_path=root_folders[0]["path"],
            )
            await sonarr.close()
            msg = f"Added '{added.get('title', title)}' to Sonarr"
            success = True

        else:
            raise ValueError(f"Unknown media type: {media_type}")

    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        # Try to extract the actual error message from the HTTP response body
        # (Sonarr/Radarr return JSON arrays like [{"errorMessage": "..."}] on 400)
        detail = str(e)
        if hasattr(e, "response"):
            try:
                body = e.response.json()
                if isinstance(body, list) and body:
                    detail = body[0].get("errorMessage") or body[0].get("message") or detail
                elif isinstance(body, dict):
                    detail = body.get("message") or body.get("errorMessage") or detail
            except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                pass
        if "already" in detail.lower() or "exists" in detail.lower():
            msg = f"'{title}' is already in your library"
            success = True
        else:
            msg = detail
            success = False

    return templates.TemplateResponse(
        request,
        "partials/discover_add_btn.html",
        {
            "success": success,
            "message": msg,
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": title,
        },
    )


@router.post("/discover/request", response_class=HTMLResponse)
async def discover_request(
    request: Request,
    asin: str = Form(...),
    title: str = Form(...),
    author: str = Form(default=""),
):
    """Submit an audiobook request to ReadMeABook."""
    if not (settings.readmeabook_url and settings.readmeabook_api_key):
        return templates.TemplateResponse(
            request,
            "partials/discover_add_btn.html",
            {
                "success": False,
                "message": "ReadMeABook is not configured",
                "media_type": "audiobook",
            },
        )
    rmab = ReadMeABookClient(settings.readmeabook_url, settings.readmeabook_api_key)
    try:
        # Check for duplicate before submitting
        existing = await rmab.get_requests()
        already = any(
            r.get("asin") == asin or r.get("title", "").lower() == title.lower() for r in existing
        )
        if already:
            return templates.TemplateResponse(
                request,
                "partials/discover_add_btn.html",
                {
                    "success": True,
                    "message": f"'{title}' is already requested",
                    "media_type": "audiobook",
                },
            )
        await rmab.create_request(asin=asin, title=title, author=author)
        return templates.TemplateResponse(
            request,
            "partials/discover_add_btn.html",
            {"success": True, "message": f"Requested '{title}'", "media_type": "audiobook"},
        )
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        return templates.TemplateResponse(
            request,
            "partials/discover_add_btn.html",
            {"success": False, "message": str(e), "media_type": "audiobook"},
        )
    finally:
        await rmab.close()
