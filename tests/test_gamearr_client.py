"""Tests for the Gamearr client."""

import httpx
import pytest

from arrmate.clients.gamearr import GamearrClient


@pytest.mark.asyncio
async def test_gamearr_unwraps_success_envelope(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url="http://gamearr:3000/api/v1/libraries",
        json={"success": True, "data": [{"id": 1, "name": "Steam"}]},
    )
    libraries = await c.get_libraries()
    assert libraries == [{"id": 1, "name": "Steam"}]
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
        json={"success": True, "data": [{"igdbId": 42, "title": "Hades"}]},
    )
    results = await c.search_games("hades")
    assert results[0]["igdbId"] == 42
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_add_game(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        method="POST",
        url="http://gamearr:3000/api/v1/games",
        match_json={"igdbId": 42, "monitored": True},
        json={"success": True, "data": {"id": 7, "igdbId": 42}},
    )
    game = await c.add_game(42)
    assert game["id"] == 7
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_grab_release(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    release = {"title": "Hades-RUNE", "downloadUrl": "http://x/y.torrent"}
    httpx_mock.add_response(
        method="POST",
        url="http://gamearr:3000/api/v1/search/grab",
        match_json={"gameId": 7, "release": release},
        json={"success": True, "data": {"releaseId": 9, "torrentHash": "abc"}},
    )
    result = await c.grab_release(7, release)
    assert result["torrentHash"] == "abc"
    await c.close()


@pytest.mark.asyncio
async def test_gamearr_get_downloads(httpx_mock):
    c = GamearrClient("http://gamearr:3000", "key")
    httpx_mock.add_response(
        url="http://gamearr:3000/api/v1/downloads",
        json={"success": True, "data": [{"hash": "abc", "progress": 50}]},
    )
    downloads = await c.get_downloads()
    assert downloads[0]["progress"] == 50
    await c.close()
