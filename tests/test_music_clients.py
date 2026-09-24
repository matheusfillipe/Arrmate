"""Lidarr and Navidrome clients parse trimmed copies of real responses."""

import json

import httpx

from arrmate.clients.lidarr import LidarrClient
from arrmate.clients.navidrome import NavidromeClient

LIDARR = "http://lidarr:8686/api/v1"
NAVIDROME = "http://navidrome:4533"

QUEUE_PAGE = {
    "page": 1,
    "totalRecords": 2,
    "records": [
        {
            "artistId": 35,
            "albumId": 3928,
            "artist": {"artistName": "Maroon 5", "foreignArtistId": "0ab49580", "id": 35},
            "quality": {"quality": {"id": 6, "name": "FLAC"}},
            "title": "Maroon 5 - Stagg Street Recordings (1999) [FLAC 16bit/44.1kHz]",
            "sizeleft": 0,
            "status": "warning",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
            "statusMessages": [],
            "errorMessage": "Downloading 1 files failed",
            "downloadId": "61eecf01323cdd15409b83ecf70ffc90",
            "downloadClient": "Slskd",
            "outputPath": "/downloads/music/Stagg-Street-Recordings-(1999)",
            "id": 1261576004,
        },
        {"albumId": 4211, "title": "Pending", "status": "delay", "sizeleft": 1, "id": 7},
    ],
}

RELEASE = {
    "guid": "29_https://rutracker.org/forum/viewtopic.php?t=2402612",
    "quality": {
        "quality": {"id": 7, "name": "ALAC"},
        "revision": {"version": 1, "real": 0, "isRepack": False},
    },
    "size": 333049629,
    "indexerId": 29,
    "indexer": "RuTracker.org (Prowlarr)",
    "title": "(House) David Guetta - Guetta Blaster - 2004, ALAC (tracks), lossless",
    "approved": False,
    "rejections": ["Existing files meets cutoff: Unknown"],
    "downloadUrl": "http://prowlarr:9696/60/download",
    "seeders": 5,
    "protocol": "TorrentDownloadProtocol",
}


async def test_lidarr_queue_keeps_unmatched_items(httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(
            f"{LIDARR}/queue",
            params={
                "pageSize": "1000",
                "includeUnknownArtistItems": "true",
                "includeArtist": "true",
            },
        ),
        json=QUEUE_PAGE,
    )
    client = LidarrClient("http://lidarr:8686", "key")
    queue = await client.get_queue()
    await client.close()
    assert [item.id for item in queue] == [1261576004, 7]
    assert queue[0].artist is not None and queue[0].artist.artist_name == "Maroon 5"
    assert queue[0].size_left == 0 and queue[1].tracked_download_state is None


async def test_lidarr_grab_posts_the_release_back_unchanged(httpx_mock):
    httpx_mock.add_response(url=f"{LIDARR}/release?albumId=1949", json=[RELEASE])
    httpx_mock.add_response(url=f"{LIDARR}/release", method="POST", json=RELEASE)
    client = LidarrClient("http://lidarr:8686", "key")
    [release] = await client.interactive_search_album(1949)
    await client.grab_release(release)
    await client.close()
    assert release.quality is not None and release.quality.quality.name == "ALAC"
    posted = httpx_mock.get_requests(method="POST")[0]
    assert json.loads(posted.content) == RELEASE


async def test_navidrome_scan_status_reads_the_subsonic_envelope(httpx_mock):
    httpx_mock.add_response(
        url=f"{NAVIDROME}/auth/login",
        method="POST",
        json={
            "id": "u1",
            "name": "Service",
            "username": "service",
            "isAdmin": True,
            "token": "jwt",
            "subsonicSalt": "salt",
            "subsonicToken": "tok",
        },
    )
    httpx_mock.add_response(
        json={
            "subsonic-response": {
                "status": "ok",
                "serverVersion": "0.60.3 (34c6f12a)",
                "scanStatus": {
                    "scanning": False,
                    "count": 43956,
                    "folderCount": 959,
                    "lastScan": "2026-09-24T10:16:45.693294855Z",
                    "scanType": "quick-selective",
                },
            }
        },
    )
    client = NavidromeClient(NAVIDROME, "service", "pw")
    status = await client.scan_status()
    await client.close()
    assert status.scanning is False and status.folder_count == 959
    assert status.last_scan is not None and status.last_scan.year == 2026
