"""Tests for the Listenarr client."""

import pytest

from arrmate.clients.listenarr import ListenarrClient

BASE = "http://listenarr:4545"


@pytest.mark.asyncio
async def test_status_uses_system_info(httpx_mock):
    """``/system/status`` is not a route in Listenarr; an unknown path returns the
    SPA's HTML, so hitting the wrong one fails silently rather than loudly."""
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/system/info", json={"version": "1.3.0"})
    assert (await c.get_system_status())["version"] == "1.3.0"
    await c.close()


@pytest.mark.asyncio
async def test_search_unwraps_indexer_results(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/search?query=dune",
        json={"indexerResults": [{"guid": "abc", "title": "Dune"}], "metadataResults": []},
    )
    results = await c.search("dune")
    assert results == [{"guid": "abc", "title": "Dune"}]
    await c.close()


@pytest.mark.asyncio
async def test_search_accepts_bare_list(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/search?query=dune", json=[{"guid": "abc"}])
    assert await c.search("dune") == [{"guid": "abc"}]
    await c.close()


@pytest.mark.asyncio
async def test_search_applies_limit(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/search?query=dune",
        json={"indexerResults": [{"guid": str(i)} for i in range(10)]},
    )
    assert len(await c.search("dune", limit=3)) == 3
    await c.close()


@pytest.mark.asyncio
async def test_queue_reads_live_client_snapshot(httpx_mock):
    """The queue comes from the download clients, not from Listenarr's own
    download records; ``/downloads`` is a different list."""
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/download/queue", json={"items": [{"id": 1, "status": "downloading"}]}
    )
    assert await c.get_queue() == [{"id": 1, "status": "downloading"}]
    await c.close()


@pytest.mark.asyncio
async def test_grab_sends_download_reference(httpx_mock):
    """A release is grabbed by its downloadReference token, not by posting the
    whole search result back."""
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/download/send", json={"id": "abc"})
    await c.grab_release("REF123", audiobook_id=7)
    sent = httpx_mock.get_requests()[0].read()
    assert b"REF123" in sent
    assert b'"audiobookId": 7' in sent or b'"audiobookId":7' in sent
    await c.close()


@pytest.mark.asyncio
async def test_add_book_wraps_metadata(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/library/add", json={"id": 1})
    await c.add_book({"asin": "B0XYZ"}, quality_profile_id=1)
    sent = httpx_mock.get_requests()[0].read()
    assert b'"metadata"' in sent
    assert b"B0XYZ" in sent
    await c.close()


@pytest.mark.asyncio
async def test_test_connection_false_on_error(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/system/info", status_code=503)
    assert await c.test_connection() is False
    await c.close()
