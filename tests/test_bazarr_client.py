"""Tests for the Bazarr client and the subtitle tool helpers."""

from urllib.parse import parse_qs

import pytest

from arrmate.agent.subtitle_tools import EpisodeSubtitles, summarize_episode
from arrmate.clients.bazarr import BazarrClient, Episode

BASE = "http://bazarr:6767"


@pytest.mark.asyncio
async def test_episodes_are_filtered_by_series_and_parsed(httpx_mock):
    client = BazarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/episodes?seriesid%5B%5D=6",
        json={"data": [{"season": 1, "episode": 2, "sonarrEpisodeId": 7, "subtitles": []}]},
    )
    [episode] = await client.get_episodes(6)
    assert (episode.season, episode.episode, episode.sonarr_episode_id) == (1, 2, 7)
    await client.close()


@pytest.mark.asyncio
async def test_history_reads_the_series_or_movie_title(httpx_mock):
    client = BazarrClient(BASE, "key")
    httpx_mock.add_response(
        url=f"{BASE}/api/movies/history?length=5", json={"data": [{"title": "Heat"}]}
    )
    [entry] = await client.get_movie_history(5)
    assert entry.title == "Heat"
    await client.close()


@pytest.mark.asyncio
async def test_series_search_is_a_form_patch(httpx_mock):
    """Bazarr parses its write endpoints as form fields; a JSON body leaves them empty."""
    client = BazarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/series", method="PATCH", status_code=204)
    await client.search_series(11)
    sent = parse_qs(httpx_mock.get_request().content.decode())
    assert sent == {"seriesid": ["11"], "action": ["search-missing"]}
    await client.close()


@pytest.mark.asyncio
async def test_clearing_a_profile_sends_an_empty_id(httpx_mock):
    client = BazarrClient(BASE, "key")
    httpx_mock.add_response(url=f"{BASE}/api/movies", method="POST", status_code=204)
    await client.set_movie_profile(33, None)
    sent = parse_qs(httpx_mock.get_request().content.decode(), keep_blank_values=True)
    assert sent == {"radarrid": ["33"], "profileid": [""]}
    await client.close()


def test_summarize_episode_marks_embedded_and_hi_tracks():
    episode = Episode.model_validate(
        {
            "season": 2,
            "episode": 9,
            "sonarrEpisodeId": 204,
            "subtitles": [
                {"code2": "en", "path": None},
                {"code2": "en", "hi": True, "path": "/tv/x.en.hi.srt"},
            ],
            "missing_subtitles": [{"code2": "pt"}],
        }
    )
    assert summarize_episode(episode) == EpisodeSubtitles(
        episode="S02E09", episode_id=204, has=["en:embedded", "en:hi"], missing=["pt"]
    )
