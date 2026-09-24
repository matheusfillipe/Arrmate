"""Tests for Cleanuparr, Jellyfin, and Jellyseerr clients (step 3/4)."""

import httpx
import pytest

from arrmate.clients.cleanuparr import CleanuparrClient
from arrmate.clients.jellyfin import JellyfinClient
from arrmate.clients.jellyseerr import JellyseerrClient, RequestStatus
from arrmate.clients.lidarr import LidarrClient


@pytest.mark.asyncio
async def test_cleanuparr_events(httpx_mock):
    c = CleanuparrClient("http://cleanuparr:11011", "k" * 64)
    httpx_mock.add_response(
        url=httpx.URL(
            "http://cleanuparr:11011/api/events",
            params={"pageSize": "50", "page": "0"},
        ),
        json={
            "items": [
                {
                    "id": "01a0d20d-d9b4-7bc2-89ad-8ae845ad3202",
                    "timestamp": "2026-09-24T06:15:18.1964702+00:00",
                    "eventType": "StalledStrike",
                    "message": "Item 'x' has been struck 4 times for reason 'Stalled'",
                    "severity": "Important",
                    "itemHash": "d74364628a793d4a93ada88555fbb17c8454f692",
                    "strikeCount": 4,
                    "failedImportReasons": [],
                }
            ],
            "page": 1,
            "pageSize": 50,
            "totalCount": 1,
            "totalPages": 1,
        },
    )
    events = await c.get_events()
    assert events[0].event_type == "StalledStrike"
    assert events[0].strike_count == 4
    await c.close()


@pytest.mark.asyncio
async def test_cleanuparr_spa_shell_detected(httpx_mock):
    c = CleanuparrClient("http://cleanuparr:11011", "k" * 64)
    httpx_mock.add_response(
        url="http://cleanuparr:11011/api/health",
        json={"__wix": {}},
    )
    assert await c.test_connection() is False
    await c.close()


@pytest.mark.asyncio
async def test_cleanuparr_health(httpx_mock):
    c = CleanuparrClient("http://cleanuparr:11011", "k" * 64)
    httpx_mock.add_response(
        url="http://cleanuparr:11011/api/health",
        json={
            "3a49": {
                "isHealthy": True,
                "lastChecked": "2026-09-24T10:20:08.4293609+00:00",
                "errorMessage": None,
                "responseTime": "00:00:06.9361512",
                "clientId": "3a49",
                "clientName": "qbittorrent",
                "clientTypeName": "qBittorrent",
            }
        },
    )
    health = await c.get_health()
    assert health["3a49"].is_healthy is True
    assert health["3a49"].client_name == "qbittorrent"
    await c.close()


@pytest.mark.asyncio
async def test_jellyfin_items(httpx_mock):
    c = JellyfinClient("http://jf:8096", "tok")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://jf:8096/Items",
            params={
                "Recursive": "true",
                "Limit": "50",
                "Fields": "Path,UserData,Overview",
                "SearchTerm": "dune",
            },
        ),
        json={
            "Items": [{"Id": "1", "Name": "Dune", "Type": "Movie", "UserData": {"Played": True}}],
            "TotalRecordCount": 1,
        },
    )
    page = await c.get_items(search_term="dune")
    assert page.items[0].name == "Dune"
    assert page.items[0].user_data and page.items[0].user_data.played is True
    await c.close()


@pytest.mark.asyncio
async def test_jellyfin_scan_posts(httpx_mock):
    c = JellyfinClient("http://jf:8096", "tok")
    httpx_mock.add_response(method="POST", url="http://jf:8096/Library/Refresh")
    await c.trigger_library_scan()
    await c.close()


@pytest.mark.asyncio
async def test_jellyseerr_requests(httpx_mock):
    c = JellyseerrClient("http://js:5055", "key")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://js:5055/api/v1/request",
            params={"take": "50", "sort": "added", "filter": "pending"},
        ),
        json={
            "pageInfo": {"pages": 1, "pageSize": 50, "results": 1, "page": 1},
            "results": [
                {
                    "id": 3,
                    "status": 1,
                    "type": "tv",
                    "is4k": False,
                    "createdAt": "2026-02-19T19:37:16.000Z",
                    "media": {"id": 9, "mediaType": "tv", "tmdbId": 125988, "status": 2},
                    "requestedBy": {"id": 1, "displayName": "mattf"},
                }
            ],
        },
    )
    page = await c.get_requests(status="pending")
    request = page.results[0]
    assert request.id == 3
    assert request.status is RequestStatus.PENDING
    assert request.media.tmdb_id == 125988
    assert request.model_dump(mode="json")["status"] == "pending"
    await c.close()


@pytest.mark.asyncio
async def test_jellyseerr_approve(httpx_mock):
    c = JellyseerrClient("http://js:5055", "key")
    httpx_mock.add_response(
        method="POST",
        url="http://js:5055/api/v1/request/3/approve",
        json={
            "id": 3,
            "status": 2,
            "type": "movie",
            "is4k": False,
            "createdAt": "2026-02-19T19:37:16.000Z",
            "media": {"id": 9, "mediaType": "movie", "tmdbId": 438631, "status": 3},
        },
    )
    approved = await c.approve_request(3)
    assert approved.status is RequestStatus.APPROVED
    await c.close()


@pytest.mark.asyncio
async def test_jellyseerr_search(httpx_mock):
    c = JellyseerrClient("http://js:5055", "key")
    httpx_mock.add_response(
        json={
            "page": 1,
            "totalPages": 1,
            "totalResults": 2,
            "results": [
                {"id": 438631, "title": "Dune", "mediaType": "movie", "releaseDate": "2021-09-15"},
                {"id": 90228, "name": "Dune: Prophecy", "mediaType": "tv"},
            ],
        },
    )
    page = await c.search_tmdb("dune prophecy")
    request = httpx_mock.get_request()
    assert request and request.url.raw_path == b"/api/v1/search?query=dune%20prophecy&page=1"
    assert [(r.media_type, r.title or r.name) for r in page.results] == [
        ("movie", "Dune"),
        ("tv", "Dune: Prophecy"),
    ]
    await c.close()


@pytest.mark.asyncio
async def test_jellyseerr_test_connection_uses_status(httpx_mock):
    """/settings/status is admin-only and 404s for an API key; /status is the public probe."""
    c = JellyseerrClient("http://js:5055", "key")
    httpx_mock.add_response(url="http://js:5055/api/v1/status", json={"version": "2.7.3"})
    assert await c.test_connection() is True
    await c.close()


@pytest.mark.asyncio
async def test_lidarr_speaks_v1(httpx_mock):
    """Lidarr's API is v1; every other *arr in the family is v3."""
    c = LidarrClient("http://lidarr:8686", "key")
    httpx_mock.add_response(
        url="http://lidarr:8686/api/v1/system/status",
        json={"appName": "Lidarr", "version": "3.1.2.4913"},
    )
    assert await c.test_connection() is True
    await c.close()
