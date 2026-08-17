"""Tests for the diagnosis client methods added for the agent (step 1)."""
import httpx
import pytest

from arrmate.clients.radarr import RadarrClient
from arrmate.clients.sonarr import SonarrClient


BASE = "http://sonarr:8989"
HEADERS = {"X-Api-Key": "key"}


@pytest.fixture
def sonarr():
    client = SonarrClient(BASE, "key")
    yield client
    client._client = None


@pytest.fixture
def radarr():
    client = RadarrClient(BASE.replace("sonarr", "radarr"), "key")
    yield client
    client._client = None


@pytest.mark.asyncio
async def test_sonarr_interactive_search_episode(sonarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}/api/v3/release",
            params={"episodeId": "42"},
        ),
        json=[{"guid": "g1", "title": "Show.S02E07.1080p", "rejections": []}],
    )
    releases = await sonarr.interactive_search_episode(42)
    assert releases[0]["guid"] == "g1"
    assert "rejections" in releases[0]


@pytest.mark.asyncio
async def test_sonarr_interactive_search_season(sonarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}/api/v3/release",
            params={"seriesId": "7", "seasonNumber": "2"},
        ),
        json=[],
    )
    assert await sonarr.interactive_search_season(7, 2) == []


@pytest.mark.asyncio
async def test_sonarr_push_release(sonarr, httpx_mock):
    release = {"guid": "g1", "indexerId": 3}
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/v3/release",
        match_json=release,
        json={"id": 1},
    )
    result = await sonarr.push_release(release)
    assert result == {"id": 1}


@pytest.mark.asyncio
async def test_sonarr_get_blocklist(sonarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}/api/v3/blocklist",
            params={"pageSize": "50", "sortKey": "date", "sortDirection": "descending"},
        ),
        json={"records": [{"title": "Bad.Release-GRP"}]},
    )
    data = await sonarr.get_blocklist()
    assert data["records"][0]["title"] == "Bad.Release-GRP"


@pytest.mark.asyncio
async def test_sonarr_episode_history_filters_by_episode(sonarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}/api/v3/history",
            params={
                "pageSize": "50",
                "episodeId": "99",
                "sortKey": "date",
                "sortDirection": "descending",
            },
        ),
        json={"records": [{"eventType": "downloadFailed", "message": "Manually marked as failed"}]},
    )
    data = await sonarr.get_episode_history(99)
    assert data["records"][0]["eventType"] == "downloadFailed"


@pytest.mark.asyncio
async def test_radarr_interactive_search(radarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(f"http://radarr:8989/api/v3/release", params={"movieId": "5"}),
        json=[{"guid": "g2", "rejections": ["Release is blocklisted"]}],
    )
    releases = await radarr.interactive_search(5)
    assert releases[0]["rejections"] == ["Release is blocklisted"]


@pytest.mark.asyncio
async def test_radarr_push_release(radarr, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="http://radarr:8989/api/v3/release",
        json={"id": 2},
    )
    assert await radarr.push_release({"guid": "g2"}) == {"id": 2}


@pytest.mark.asyncio
async def test_radarr_get_blocklist(radarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            "http://radarr:8989/api/v3/blocklist",
            params={"pageSize": "50", "sortKey": "date", "sortDirection": "descending"},
        ),
        json={"records": []},
    )
    assert (await radarr.get_blocklist())["records"] == []


@pytest.mark.asyncio
async def test_radarr_movie_history(radarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            "http://radarr:8989/api/v3/history",
            params={
                "pageSize": "50",
                "movieId": "5",
                "sortKey": "date",
                "sortDirection": "descending",
            },
        ),
        json={"records": [{"eventType": "grabbed"}]},
    )
    data = await radarr.get_movie_history(5)
    assert data["records"][0]["eventType"] == "grabbed"
