"""Tests for the diagnosis client methods the agent uses."""

import httpx
import pytest

from arrmate.clients.base_arr import Release
from arrmate.clients.radarr import RadarrClient
from arrmate.clients.sonarr import SonarrClient

BASE = "http://sonarr:8989"

QUALITY = {"quality": {"id": 7, "name": "Bluray-1080p"}}


def release_json(guid: str, rejections: list[str]) -> dict[str, object]:
    return {
        "guid": guid,
        "indexerId": 3,
        "indexer": "Indexer",
        "title": "Show.S02E07.1080p",
        "size": 1_000_000,
        "protocol": "torrent",
        "quality": QUALITY,
        "approved": not rejections,
        "rejected": bool(rejections),
        "rejections": rejections,
        "seeders": 12,
    }


def history_json(event_type: str, message: str | None = None) -> dict[str, object]:
    return {
        "id": 1,
        "sourceTitle": "Show.S02E07.1080p",
        "date": "2026-09-01T10:00:00Z",
        "quality": QUALITY,
        "eventType": event_type,
        "downloadId": "ABC",
        "data": {"message": message} if message else {},
        "episodeId": 99,
        "seriesId": 7,
        "movieId": 5,
    }


def page(records: list[dict[str, object]]) -> dict[str, object]:
    return {"page": 1, "pageSize": 50, "totalRecords": len(records), "records": records}


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
        json=[release_json("g1", [])],
    )
    releases = await sonarr.interactive_search_episode(42)
    assert releases[0].guid == "g1"
    assert releases[0].indexer_id == 3
    assert releases[0].quality.quality.name == "Bluray-1080p"


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
async def test_push_release_sends_only_what_the_service_keys_a_grab_on(sonarr, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url=f"{BASE}/api/v3/release",
        match_json={"guid": "g1", "indexerId": 3},
        json={"guid": "g1", "indexerId": 3},
    )
    await sonarr.push_release(Release.model_validate(release_json("g1", [])))


@pytest.mark.asyncio
async def test_sonarr_get_blocklist(sonarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{BASE}/api/v3/blocklist",
            params={"pageSize": "50", "sortKey": "date", "sortDirection": "descending"},
        ),
        json=page(
            [
                {
                    "id": 4,
                    "sourceTitle": "Bad.Release-GRP",
                    "date": "2026-09-01T10:00:00Z",
                    "protocol": "torrent",
                    "quality": QUALITY,
                    "message": "Manually marked as failed",
                }
            ]
        ),
    )
    blocklist = await sonarr.get_blocklist()
    assert blocklist.records[0].source_title == "Bad.Release-GRP"


@pytest.mark.asyncio
async def test_history_message_comes_from_the_event_data(sonarr, httpx_mock):
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
        json=page([history_json("downloadFailed", "Manually marked as failed")]),
    )
    history = await sonarr.get_episode_history(99)
    assert history.records[0].event_type == "downloadFailed"
    assert history.records[0].message == "Manually marked as failed"


@pytest.mark.asyncio
async def test_radarr_interactive_search_keeps_rejections(radarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL("http://radarr:8989/api/v3/release", params={"movieId": "5"}),
        json=[release_json("g2", ["Release is blocklisted"])],
    )
    releases = await radarr.interactive_search(5)
    assert releases[0].rejections == ["Release is blocklisted"]


@pytest.mark.asyncio
async def test_radarr_get_blocklist(radarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            "http://radarr:8989/api/v3/blocklist",
            params={"pageSize": "50", "sortKey": "date", "sortDirection": "descending"},
        ),
        json=page([]),
    )
    assert (await radarr.get_blocklist()).records == []


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
        json=page([history_json("grabbed")]),
    )
    history = await radarr.get_movie_history(5)
    assert history.records[0].event_type == "grabbed"
    assert history.records[0].message is None
