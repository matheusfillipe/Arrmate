"""Tests for downloader clients: file lists, queues and actions."""

import httpx
import pytest

from arrmate.agent.playbooks import _looks_poisoned
from arrmate.clients.nzbget import NZBgetClient
from arrmate.clients.qbittorrent import QBittorrentClient
from arrmate.clients.sabnzbd import SABnzbdClient
from arrmate.clients.transmission import TorrentStatus, TransmissionClient


@pytest.fixture
def qbit():
    client = QBittorrentClient("http://qb:8080", "u", "p")
    yield client
    client._client = None


@pytest.mark.asyncio
async def test_qbit_files(qbit, httpx_mock):
    httpx_mock.add_response(url=httpx.URL("http://qb:8080/api/v2/auth/login"), text="Ok.")
    httpx_mock.add_response(
        url=httpx.URL("http://qb:8080/api/v2/torrents/files", params={"hash": "abc"}),
        json=[
            {
                "availability": 1,
                "index": 0,
                "name": "X-Men.97.S02E07.1080p.WEB.h264-GRACE.exe",
                "piece_range": [0, 10],
                "priority": 1,
                "progress": 1,
                "size": 959000000,
            }
        ],
    )
    files = await qbit.get_item_files("abc")
    assert files[0].name.endswith(".exe")
    assert _looks_poisoned([f.name for f in files]) is not None


@pytest.mark.asyncio
async def test_qbit_torrents_and_transfer(qbit, httpx_mock):
    httpx_mock.add_response(url="http://qb:8080/api/v2/auth/login", text="Ok.")
    httpx_mock.add_response(
        url="http://qb:8080/api/v2/torrents/info",
        json=[
            {
                "hash": "d743",
                "name": "Album [FLAC]",
                "state": "stalledDL",
                "progress": 0.5,
                "size": 100,
                "total_size": 200,
                "dlspeed": 0,
                "num_seeds": 0,
                "category": "lidarr",
                "private": None,
            }
        ],
    )
    httpx_mock.add_response(
        url="http://qb:8080/api/v2/transfer/info",
        json={
            "connection_status": "connected",
            "dl_info_speed": 947968,
            "up_info_speed": 0,
            "dl_info_data": 1,
            "up_info_data": 0,
            "dl_rate_limit": 0,
            "up_rate_limit": 0,
            "dht_nodes": 352,
        },
    )
    torrents = await qbit.get_torrents()
    assert torrents[0].state == "stalledDL"
    assert torrents[0].total_size == 200
    assert (await qbit.get_transfer_info()).dl_info_speed == 947968


@pytest.mark.asyncio
async def test_qbit_unknown_state_keeps_the_list(qbit, httpx_mock):
    httpx_mock.add_response(url="http://qb:8080/api/v2/auth/login", text="Ok.")
    httpx_mock.add_response(
        url="http://qb:8080/api/v2/torrents/info",
        json=[
            {"hash": "a1", "name": "One", "state": "futureState", "progress": 0, "size": 1},
            {"hash": "b2", "name": "Two", "state": "uploading", "progress": 1, "size": 1},
        ],
    )
    torrents = await qbit.get_torrents()
    assert [t.state for t in torrents] == ["futureState", "uploading"]


@pytest.mark.asyncio
async def test_qbit_recheck_reannounce(qbit, httpx_mock):
    httpx_mock.add_response(url="http://qb:8080/api/v2/auth/login", text="Ok.")
    httpx_mock.add_response(url="http://qb:8080/api/v2/torrents/recheck", text="Ok.")
    httpx_mock.add_response(url="http://qb:8080/api/v2/torrents/reannounce", text="Ok.")
    assert await qbit.recheck_torrent("abc") is True
    assert await qbit.reannounce_torrent("abc") is True


@pytest.mark.asyncio
async def test_qbit_priority_moves(qbit, httpx_mock):
    httpx_mock.add_response(url="http://qb:8080/api/v2/auth/login", text="Ok.")
    httpx_mock.add_response(url="http://qb:8080/api/v2/torrents/topPrio", text="")
    assert await qbit.set_priority("abc", "top") is True


@pytest.mark.asyncio
async def test_transmission_files_and_torrents(httpx_mock):
    client = TransmissionClient("http://tr:9091")
    httpx_mock.add_response(
        url="http://tr:9091/transmission/rpc",
        json={
            "result": "success",
            "arguments": {
                "torrents": [
                    {
                        "id": 1,
                        "name": "t",
                        "status": 4,
                        "percentDone": 0.25,
                        "totalSize": 400,
                        "files": [{"name": "a.mkv", "length": 100, "bytesCompleted": 25}],
                    }
                ]
            },
        },
        is_reusable=True,
    )
    files = await client.get_item_files(1)
    assert [(f.name, f.length) for f in files] == [("a.mkv", 100)]
    torrents = await client.get_torrents()
    assert torrents[0].status is TorrentStatus.DOWNLOADING
    await client.close()


@pytest.mark.asyncio
async def test_transmission_failed_result_is_not_success(httpx_mock):
    client = TransmissionClient("http://tr:9091")
    httpx_mock.add_response(
        url="http://tr:9091/transmission/rpc", json={"result": "invalid argument"}
    )
    assert await client.pause_torrent(1) is False
    await client.close()


@pytest.mark.asyncio
async def test_sabnzbd_files(httpx_mock):
    client = SABnzbdClient("http://sab:8080", "key")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://sab:8080/api",
            params={"apikey": "key", "output": "json", "mode": "get_files", "value": "n1"},
        ),
        json={
            "files": [
                {
                    "status": "finished",
                    "mbleft": "0.00",
                    "mb": "0.05",
                    "age": "25d",
                    "bytes": "52161.00",
                    "filename": "r01.par2",
                    "nzf_id": "SABnzbd_nzf_1lk0ij",
                }
            ]
        },
    )
    files = await client.get_item_files("n1")
    assert files[0].filename == "r01.par2"
    assert files[0].bytes == 52161
    await client.close()


@pytest.mark.asyncio
async def test_sabnzbd_queue(httpx_mock):
    client = SABnzbdClient("http://sab:8080", "key")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://sab:8080/api", params={"apikey": "key", "output": "json", "mode": "queue"}
        ),
        json={
            "queue": {
                "status": "Downloading",
                "speedlimit_abs": "4718592.0",
                "paused": False,
                "kbpersec": "1296.02",
                "slots": [
                    {
                        "status": "Downloading",
                        "index": 0,
                        "filename": "TV.Show.S04E11.720p.HDTV.x264",
                        "size": "1.2 GB",
                        "percentage": "3",
                        "priority": "Normal",
                        "cat": "tv",
                        "nzo_id": "SABnzbd_nzo_p86tgx",
                    }
                ],
            }
        },
    )
    queue = await client.get_queue()
    assert queue.kbpersec == pytest.approx(1296.02)
    assert queue.slots[0].percentage == 3
    await client.close()


@pytest.mark.asyncio
async def test_nzbget_files(httpx_mock):
    client = NZBgetClient("http://nzb:6789", "u", "p")
    httpx_mock.add_response(
        url="http://nzb:6789/jsonrpc",
        json={
            "result": [
                {"ID": 3, "NZBID": 10, "Filename": "f.rar", "FileSizeLo": 7, "FileSizeHi": 1}
            ]
        },
    )
    files = await client.get_item_files(10)
    assert files[0].filename == "f.rar"
    assert files[0].size == (1 << 32) + 7
    await client.close()


@pytest.mark.asyncio
async def test_nzbget_queue_and_status(httpx_mock):
    client = NZBgetClient("http://nzb:6789", "u", "p")
    httpx_mock.add_response(
        url="http://nzb:6789/jsonrpc",
        match_json={"method": "listgroups", "params": [0], "id": 1},
        json={
            "result": [
                {
                    "NZBID": 10,
                    "NZBName": "Show.S01E01",
                    "Status": "DOWNLOADING",
                    "FileSizeMB": 1000,
                    "RemainingSizeMB": 250,
                }
            ]
        },
    )
    httpx_mock.add_response(
        url="http://nzb:6789/jsonrpc",
        match_json={"method": "status", "params": [], "id": 1},
        json={"result": {"DownloadRateLo": 2048, "DownloadRateHi": 0, "DownloadPaused": True}},
    )
    groups = await client.get_queue()
    assert groups[0].percent_done == 75
    status = await client.get_status()
    assert status.download_rate == 2048
    assert status.download_paused is True
    await client.close()


def test_single_video_file_is_clean():
    assert _looks_poisoned(["Show.S01E01.mkv"]) is None
    assert _looks_poisoned(["readme.txt"]) is not None
