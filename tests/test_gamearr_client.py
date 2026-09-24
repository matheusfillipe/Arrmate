"""Tests for the Gamearr client."""

import httpx
import pytest

from arrmate.clients.gamearr import GamearrClient, GameRelease

GAME = {
    "id": 7,
    "igdbId": 42,
    "title": "Hades",
    "slug": "hades",
    "year": 2020,
    "platform": "PC (Microsoft Windows)",
    "store": None,
    "monitored": True,
    "status": "wanted",
    "libraryId": 1,
    "folderPath": None,
    "updateAvailable": False,
    "stores": [],
    "folders": [],
}


@pytest.mark.asyncio
async def test_gamearr_unwraps_success_envelope(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url="http://gamearr:3000/api/v1/libraries",
        json={
            "success": True,
            "data": [
                {
                    "id": 1,
                    "name": "Main Library",
                    "path": "/roms/gamearr",
                    "platform": None,
                    "monitored": True,
                    "priority": 0,
                }
            ],
        },
    )
    libraries = await c.get_libraries()
    assert libraries[0].name == "Main Library"
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_raises_on_error_envelope(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url="http://gamearr:3000/api/v1/libraries",
        json={"success": False, "error": "Not configured", "code": 2000},
    )
    with pytest.raises(ValueError, match="Not configured"):
        await c.get_libraries()
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_test_connection(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url="http://gamearr:3000/api/v1/system/status",
        json={"success": True, "data": {"status": "healthy", "version": "1.0.0"}},
    )
    assert await c.test_connection() is True
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_test_connection_fails(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(url="http://gamearr:3000/api/v1/system/status", status_code=503)
    assert await c.test_connection() is False
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_search_games(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url=httpx.URL("http://gamearr:3000/api/v1/search/games", params={"q": "hades"}),
        json={
            "success": True,
            "data": [
                {
                    "igdbId": 42,
                    "title": "Hades",
                    "platforms": ["PC (Microsoft Windows)"],
                    "existingGameId": None,
                }
            ],
        },
    )
    results = await c.search_games("hades")
    assert results[0].igdb_id == 42
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_add_game(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        method="POST",
        url="http://gamearr:3000/api/v1/games",
        match_json={"igdbId": 42, "monitored": True},
        json={"success": True, "data": GAME},
    )
    game = await c.add_game(42)
    assert game.id == 7
    assert game.status == "wanted"
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_grab_sends_the_release_back_as_received(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    raw = {
        "guid": "nps:PSV:PCSE00011:GAMES",
        "title": "Silent Hill [PCSE00011]",
        "indexer": "NoPayStation",
        "size": 1581727920,
        "seeders": 0,
        "leechers": 0,
        "downloadUrl": "http://zeus.dl.playstation.net/x.pkg",
        "publishedAt": "2026-09-24T10:22:49.465Z",
        "protocol": "direct",
        "score": 200,
    }
    release = GameRelease.model_validate(raw | {"matchConfidence": "high"})
    httpx_mock.add_response(
        method="POST",
        url="http://gamearr:3000/api/v1/search/grab",
        match_json={"gameId": 7, "release": raw},
        json={"success": True, "data": {"releaseId": 9, "torrentHash": "abc"}},
    )
    result = await c.grab_release(7, release)
    assert result.torrent_hash == "abc"
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_get_downloads(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url="http://gamearr:3000/api/v1/downloads",
        json={
            "success": True,
            "data": [
                {
                    "hash": "abc",
                    "name": "Hades",
                    "progress": 0.5,
                    "downloadSpeed": 1024,
                    "eta": 60,
                    "state": "downloading",
                    "gameId": 7,
                    "client": "qbittorrent",
                }
            ],
        },
    )
    downloads = await c.get_downloads()
    assert downloads[0].progress == 0.5
    assert downloads[0].download_speed == 1024
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_works_without_an_api_key(httpx_mock):
    """Gamearr only enforces a key once an admin account exists."""
    c = GamearrClient("http://gamearr:8484", "")
    httpx_mock.add_response(
        url="http://gamearr:8484/api/v1/system/status",
        json={"success": True, "data": {"status": "healthy"}},
    )
    assert await c.test_connection() is True
    await c.close()


def test_gamearr_is_configured_by_url_alone(monkeypatch):
    from arrmate.clients import discovery

    spec = discovery.SERVICE_REGISTRY["gamearr"]
    monkeypatch.setattr(discovery.settings, "gamearr_url", "http://gamearr:8484")
    monkeypatch.setattr(discovery.settings, "gamearr_api_key", None)
    assert discovery._is_configured(spec) is True

    monkeypatch.setattr(discovery.settings, "gamearr_url", "")
    assert discovery._is_configured(spec) is False
