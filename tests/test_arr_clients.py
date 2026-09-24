"""Parsing and write-back tests for the Sonarr and Radarr clients."""

import json

import httpx
import pytest

from arrmate.clients.base_arr import BaseArrClient
from arrmate.clients.sonarr import Series, SonarrClient

BASE = "http://sonarr:8989"

SERIES = {
    "id": 7,
    "title": "Show",
    "year": 2020,
    "status": "continuing",
    "seriesType": "standard",
    "tvdbId": 81189,
    "tmdbId": 1396,
    "monitored": True,
    "path": "/tv/Show",
    "qualityProfileId": 1,
    "seasons": [
        {"seasonNumber": 1, "monitored": True},
        {"seasonNumber": 2, "monitored": True},
    ],
    "titleSlug": "show",
    "languageProfileId": 1,
}


@pytest.fixture
def sonarr():
    client = SonarrClient(BASE, "key")
    yield client
    client._client = None


@pytest.mark.asyncio
async def test_season_monitoring_puts_back_fields_the_model_drops(sonarr, httpx_mock):
    httpx_mock.add_response(url=f"{BASE}/api/v3/series/7", json=SERIES)
    httpx_mock.add_response(method="PUT", url=f"{BASE}/api/v3/series/7", json=SERIES)

    await sonarr.set_season_monitored(7, season=2, monitored=False)

    sent = json.loads(httpx_mock.get_requests(method="PUT")[0].content)
    assert sent["titleSlug"] == "show"
    assert sent["languageProfileId"] == 1
    assert [s["monitored"] for s in sent["seasons"]] == [True, False]


@pytest.mark.asyncio
async def test_add_series_posts_the_lookup_with_our_settings(sonarr, httpx_mock):
    httpx_mock.add_response(
        url=httpx.URL(f"{BASE}/api/v3/series/lookup", params={"term": "tvdb:81189"}),
        json=[{**SERIES, "id": None}],
    )
    httpx_mock.add_response(method="POST", url=f"{BASE}/api/v3/series", json=SERIES)

    added = await sonarr.add_series(81189, quality_profile_id=4, root_folder_path="/tv")

    sent = json.loads(httpx_mock.get_requests(method="POST")[0].content)
    assert sent["titleSlug"] == "show"
    assert (sent["qualityProfileId"], sent["rootFolderPath"]) == (4, "/tv")
    assert sent["addOptions"] == {"searchForMissingEpisodes": True}
    assert added == Series.model_validate(SERIES)


@pytest.mark.asyncio
async def test_a_subclass_without_type_arguments_gets_raw_json(httpx_mock):
    class UntypedClient(BaseArrClient):
        entity = "artist"

    client = UntypedClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/v3/artist/3", json={"id": 3, "artistName": "A"})
    assert await client.get_item(3) == {"id": 3, "artistName": "A"}
    client._client = None
