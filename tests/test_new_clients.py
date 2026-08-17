"""Tests for Cleanuparr, Jellyfin, and Jellyseerr clients (step 3/4)."""
import httpx
import pytest

from arrmate.clients.cleanuparr import CleanuparrClient
from arrmate.clients.jellyfin import JellyfinClient
from arrmate.clients.jellyseerr import JellyseerrClient


@pytest.mark.asyncio
async def test_cleanuparr_events(httpx_mock):
    c = CleanuparrClient("http://cleanuparr:11011", "k" * 64)
    httpx_mock.add_response(
        url=httpx.URL(
            "http://cleanuparr:11011/api/events",
            params={"pageSize": "50", "page": "0"},
        ),
        json={"items": [{"id": 1, "action": "struck"}]},
    )
    events = await c.get_events()
    assert events[0]["action"] == "struck"
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
        json={"qbit": "up"},
    )
    assert await c.get_health() == {"qbit": "up"}
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
        json={"Items": [{"Id": "1", "Name": "Dune", "Type": "Movie"}]},
    )
    data = await c.get_items(search_term="dune")
    assert data["Items"][0]["Name"] == "Dune"
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
        json={"results": [{"id": 3, "media": {"title": "Silo"}}]},
    )
    data = await c.get_requests(status="pending")
    assert data["results"][0]["id"] == 3
    await c.close()


@pytest.mark.asyncio
async def test_jellyseerr_approve(httpx_mock):
    c = JellyseerrClient("http://js:5055", "key")
    httpx_mock.add_response(
        method="POST", url="http://js:5055/api/v1/request/3/approve", json={"success": True}
    )
    await c.approve_request(3)
    await c.close()


@pytest.mark.asyncio
async def test_jellyseerr_search(httpx_mock):
    c = JellyseerrClient("http://js:5055", "key")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://js:5055/api/v1/search",
            params={"query": "dune", "page": "1"},
        ),
        json={"results": [{"id": 429, "name": "Dune", "mediaType": "movie"}]},
    )
    data = await c.search_tmdb("dune")
    assert data["results"][0]["mediaType"] == "movie"
    await c.close()
