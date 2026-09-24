"""Parsing tests for the LazyLibrarian, AudioBookshelf, ReadMeABook and Open Library clients."""

import json

import httpx
import pytest

from arrmate.clients.audiobookshelf import AudioBookshelfClient
from arrmate.clients.lazylibrarian import LazyLibrarianClient
from arrmate.clients.openlibrary import OpenLibraryClient
from arrmate.clients.readmeabook import ReadMeABookClient

LL = "http://lazylibrarian:5299"
ABS = "http://audiobookshelf:13378"
RMAB = "http://readmeabook:3030"


def _ll_url(cmd: str, **params: str) -> httpx.URL:
    return httpx.URL(f"{LL}/api", params={"apikey": "key", "cmd": cmd, **params})


@pytest.mark.asyncio
async def test_lazylibrarian_version_reads_current_version(httpx_mock):
    c = LazyLibrarianClient(LL, "key")
    httpx_mock.add_response(
        url=_ll_url("getVersion"),
        json={"Success": True, "current_version": "abc123", "commits_behind": 0},
    )
    status = await c.get_system_status()
    assert (status.success, status.version) == (True, "abc123")
    await c.close()


@pytest.mark.asyncio
async def test_lazylibrarian_index_is_a_bare_list_of_author_rows(httpx_mock):
    c = LazyLibrarianClient(LL, "key")
    httpx_mock.add_response(
        url=_ll_url("getIndex"),
        json=[{"AuthorID": "OL1A", "AuthorName": "Andy Weir", "Status": "Active"}],
    )
    [author] = await c.get_all_authors()
    assert (author.author_id, author.author_name) == ("OL1A", "Andy Weir")
    await c.close()


@pytest.mark.asyncio
async def test_lazylibrarian_query_values_are_encoded_once(httpx_mock):
    c = LazyLibrarianClient(LL, "key")
    httpx_mock.add_response(url=_ll_url("findBook", name="Project Hail Mary"), json=[])
    assert await c.find_book("Project Hail Mary") == []
    await c.close()


@pytest.mark.asyncio
async def test_lazylibrarian_action_commands_return_the_text_reply(httpx_mock):
    c = LazyLibrarianClient(LL, "key")
    httpx_mock.add_response(url=_ll_url("queueBook", id="B1", type="AudioBook"), text="OK")
    assert await c.queue_book("B1", "AudioBook") == "OK"
    await c.close()


@pytest.mark.asyncio
async def test_audiobookshelf_version_comes_from_status(httpx_mock):
    c = AudioBookshelfClient(ABS, "token")
    httpx_mock.add_response(
        url=f"{ABS}/status", json={"app": "audiobookshelf", "serverVersion": "2.17.0"}
    )
    assert (await c.get_system_status()).version == "2.17.0"
    await c.close()


@pytest.mark.asyncio
async def test_audiobookshelf_search_covers_book_libraries_only(httpx_mock):
    c = AudioBookshelfClient(ABS, "token")
    httpx_mock.add_response(
        url=f"{ABS}/api/libraries",
        json={
            "libraries": [
                {"id": "lib-books", "name": "Books", "mediaType": "book"},
                {"id": "lib-pods", "name": "Podcasts", "mediaType": "podcast"},
            ]
        },
    )
    httpx_mock.add_response(
        url=f"{ABS}/api/libraries/lib-books/search?q=dune",
        json={
            "book": [
                {
                    "libraryItem": {
                        "id": "li-1",
                        "mediaType": "book",
                        "media": {"metadata": {"title": "Dune", "authorName": "Frank Herbert"}},
                    }
                }
            ],
            "authors": [],
        },
    )
    [item] = await c.search("dune")
    assert (item.id, item.media.metadata.author_name) == ("li-1", "Frank Herbert")
    await c.close()


@pytest.mark.asyncio
async def test_readmeabook_request_matches_nested_audiobook(httpx_mock):
    c = ReadMeABookClient(RMAB, "token")
    httpx_mock.add_response(
        url=f"{RMAB}/api/requests",
        json={
            "success": True,
            "requests": [
                {
                    "id": "r1",
                    "status": "downloading",
                    "audiobook": {"title": "Dune", "author": "F", "audibleAsin": "B002V1OF70"},
                }
            ],
        },
    )
    [req] = await c.get_requests()
    assert req.matches("B002V1OF70", "other")
    assert req.matches("X", "dune")
    assert not req.matches("X", "other")
    await c.close()


@pytest.mark.asyncio
async def test_readmeabook_create_request_wraps_the_audiobook(httpx_mock):
    c = ReadMeABookClient(RMAB, "token")
    httpx_mock.add_response(
        url=f"{RMAB}/api/requests",
        method="POST",
        json={"success": True, "request": {"id": "r2", "status": "pending"}},
    )
    created = await c.create_request(asin="B0", title="Dune", author="Frank Herbert")
    sent = json.loads(httpx_mock.get_requests()[0].read())
    assert sent == {"audiobook": {"asin": "B0", "title": "Dune", "author": "Frank Herbert"}}
    assert created.status == "pending"
    await c.close()


@pytest.mark.asyncio
async def test_readmeabook_popular_reads_cover_art_url(httpx_mock):
    c = ReadMeABookClient(RMAB, "token")
    httpx_mock.add_response(
        url=f"{RMAB}/api/audiobooks/popular",
        json={
            "audiobooks": [{"asin": "B0", "title": "Dune", "author": "F", "coverArtUrl": "/c.jpg"}]
        },
    )
    [book] = await c.get_popular()
    assert book.cover_art_url == "/c.jpg"
    await c.close()


@pytest.mark.asyncio
async def test_openlibrary_trending_becomes_cards(httpx_mock):
    c = OpenLibraryClient()
    httpx_mock.add_response(
        url="https://openlibrary.org/trending/daily.json?limit=24",
        json={
            "works": [
                {
                    "key": "/works/OL1W",
                    "title": "Atomic Habits",
                    "author_name": ["James Clear"],
                    "first_publish_year": 2016,
                    "cover_i": 12539702,
                }
            ]
        },
    )
    [card] = await c.get_trending_daily()
    assert (card.display_title, card.author, card.year) == ("Atomic Habits", "James Clear", "2016")
    assert card.poster == "https://covers.openlibrary.org/b/id/12539702-M.jpg"
    await c.close()
