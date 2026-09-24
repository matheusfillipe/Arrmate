"""Tests for the Listenarr client."""

import pytest

from arrmate.clients.listenarr import AudibleMetadata, ListenarrClient

BASE = "http://listenarr:4545"


@pytest.mark.asyncio
async def test_status_uses_system_info(httpx_mock):
    """``/system/status`` is not a route in Listenarr; an unknown path returns the
    SPA's HTML, so hitting the wrong one fails silently rather than loudly."""
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/system/info", json={"version": "1.3.0"})
    assert (await c.get_system_status()).version == "1.3.0"
    await c.close()


@pytest.mark.asyncio
async def test_search_unwraps_indexer_results(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/search?query=dune",
        json={
            "indexerResults": [{"title": "Dune", "downloadReference": "REF", "ageHours": 1.5}],
            "metadataResults": [],
        },
    )
    [release] = await c.search("dune")
    assert (release.title, release.download_reference, release.age_hours) == ("Dune", "REF", 1.5)
    await c.close()


@pytest.mark.asyncio
async def test_search_accepts_bare_list(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/search?query=dune", json=[{"title": "Dune"}])
    assert [r.title for r in await c.search("dune")] == ["Dune"]
    await c.close()


@pytest.mark.asyncio
async def test_search_applies_limit(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/search?query=dune",
        json={"indexerResults": [{"title": str(i)} for i in range(10)]},
    )
    assert len(await c.search("dune", limit=3)) == 3
    await c.close()


@pytest.mark.asyncio
async def test_queue_reads_live_client_snapshot(httpx_mock):
    """The queue comes from the download clients, not from Listenarr's own
    download records; ``/downloads`` is a different list."""
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/download/queue",
        json={
            "items": [
                {
                    "id": "54225f4f",
                    "title": "Red Rising",
                    "status": "downloading",
                    "progress": 40,
                    "downloadClient": "qBittorrent",
                    "audiobookId": 10,
                }
            ]
        },
    )
    [item] = await c.get_queue()
    assert (item.id, item.status, item.download_client, item.audiobook_id) == (
        "54225f4f",
        "downloading",
        "qBittorrent",
        10,
    )
    await c.close()


@pytest.mark.asyncio
async def test_library_parses_authors_and_status(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/library",
        json=[
            {
                "id": 3,
                "title": "Der Marsianer",
                "authors": ["Andy Weir"],
                "narrators": ["Richard Barenberg"],
                "monitored": True,
                "status": "no-file",
                "fileCount": 0,
                "genres": ["Literature & Fiction"],
            }
        ],
    )
    [book] = await c.get_all_items()
    assert (book.authors, book.status, book.file_count) == (["Andy Weir"], "no-file", 0)
    await c.close()


@pytest.mark.asyncio
async def test_grab_sends_download_reference(httpx_mock):
    """A release is grabbed by its downloadReference token, not by posting the
    whole search result back."""
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/download/send", json={"downloadId": "abc", "message": "sent"}
    )
    result = await c.grab_release("REF123", audiobook_id=7)
    sent = httpx_mock.get_requests()[0].read()
    assert b"REF123" in sent
    assert b'"audiobookId": 7' in sent or b'"audiobookId":7' in sent
    assert b"downloadClientId" not in sent
    assert result.download_id == "abc"
    await c.close()


@pytest.mark.asyncio
async def test_add_book_wraps_metadata_in_camel_case(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/v1/library/add",
        json={"message": "added", "audiobook": {"id": 1, "title": "Dune", "monitored": True}},
    )
    metadata = AudibleMetadata.model_validate_json('{"asin": "B0XYZ", "series_number": "1"}')
    result = await c.add_book(metadata, quality_profile_id=1)
    sent = httpx_mock.get_requests()[0].read()
    assert b'"metadata"' in sent
    assert b"B0XYZ" in sent
    assert b'"seriesNumber"' in sent
    assert result.audiobook is not None
    assert result.audiobook.id == 1
    await c.close()


@pytest.mark.asyncio
async def test_test_connection_false_on_error(httpx_mock):
    c = ListenarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v1/system/info", status_code=503)
    assert await c.test_connection() is False
    await c.close()
