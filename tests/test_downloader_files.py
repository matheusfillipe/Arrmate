"""Tests for downloader get_item_files and actions (step 1b)."""
import httpx
import pytest

from arrmate.clients.nzbget import NZBgetClient
from arrmate.clients.qbittorrent import QBittorrentClient
from arrmate.clients.sabnzbd import SABnzbdClient
from arrmate.clients.transmission import TransmissionClient


@pytest.fixture
def qbit():
    client = QBittorrentClient("http://qb:8080", "u", "p")
    yield client
    client._client = None


@pytest.mark.asyncio
async def test_qbit_files(qbit, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(f"http://qb:8080/api/v2/auth/login"),
        text="Ok.",
    )
    httpx_mock.add_response(
        url=httpx.URL(f"http://qb:8080/api/v2/torrents/files", params={"hash": "abc"}),
        json=[{"name": "X-Men.97.S02E07.1080p.WEB.h264-GRACE.exe", "size": 959000000}],
    )
    files = await qbit.get_item_files("abc")
    assert files[0]["name"].endswith(".exe")


@pytest.mark.asyncio
async def test_qbit_recheck_reannounce(qbit, httpx_mock):
    httpx_mock.add_response(url="http://qb:8080/api/v2/auth/login", text="Ok.")
    httpx_mock.add_response(url="http://qb:8080/api/v2/torrents/recheck", text="Ok.")
    httpx_mock.add_response(url="http://qb:8080/api/v2/torrents/reannounce", text="Ok.")
    assert await qbit.recheck_torrent("abc") is True
    assert await qbit.reannounce_torrent("abc") is True


@pytest.mark.asyncio
async def test_transmission_files(httpx_mock):
    client = TransmissionClient("http://tr:9091")
    httpx_mock.add_response(
        url="http://tr:9091/transmission/rpc",
        json={
            "result": "success",
            "arguments": {
                "torrents": [
                    {"id": 1, "name": "t", "files": [{"name": "a.mkv", "length": 100}]}
                ]
            },
        },
    )
    files = await client.get_item_files(1)
    assert files == [{"name": "a.mkv", "size": 100}]
    await client.close()


@pytest.mark.asyncio
async def test_sabnzbd_files(httpx_mock):
    client = SABnzbdClient("http://sab:8080", "key")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://sab:8080/api",
            params={"apikey": "key", "output": "json", "mode": "files", "value": "n1"},
        ),
        json={"files": [{"filename": "r01", "bytes": 5, "status": "done"}]},
    )
    files = await client.get_item_files("n1")
    assert files[0]["name"] == "r01"
    await client.close()


@pytest.mark.asyncio
async def test_nzbget_files(httpx_mock):
    client = NZBgetClient("http://nzb:6789", "u", "p")
    httpx_mock.add_response(
        url="http://nzb:6789/jsonrpc",
        json={"result": [{"FileName": "f.nzb", "FileSize": 7}]},
    )
    files = await client.get_item_files(10)
    assert files == [{"name": "f.nzb", "size": 7}]
    await client.close()
