"""Parsing tests for the Plex, plex.tv, TMDB and Last.fm clients."""

import httpx

from arrmate.clients.lastfm import LastFMClient
from arrmate.clients.plex import PlexClient
from arrmate.clients.plex_tv import PlexTVClient
from arrmate.clients.tmdb import TMDBClient


async def test_plex_session_parses_nested_objects(httpx_mock):
    c = PlexClient("http://plex:32400", "tok")
    httpx_mock.add_response(
        json={
            "MediaContainer": {
                "size": 1,
                "Metadata": [
                    {
                        "ratingKey": "42",
                        "type": "episode",
                        "title": "Pilot",
                        "grandparentTitle": "Silo",
                        "parentIndex": 1,
                        "index": 2,
                        "duration": 1000,
                        "viewOffset": 250,
                        "sessionKey": "7",
                        "User": {"id": "1", "title": "mattf"},
                        "Player": {"title": "TV", "state": "paused"},
                        "Session": {"id": "abc", "bandwidth": 4000},
                        "Media": [{"videoCodec": "hevc", "width": 3840, "height": 2160}],
                    }
                ],
            }
        },
    )
    [session] = await c.get_sessions()
    assert session.episode_code == "S01E02"
    assert session.progress_pct == 25
    assert session.user and session.user.title == "mattf"
    assert session.session and session.session.id == "abc"
    assert session.media[0].video_codec == "hevc"
    await c.close()


async def test_plex_history_and_accounts(httpx_mock):
    c = PlexClient("http://plex:32400", "tok")
    httpx_mock.add_response(
        url=httpx.URL(
            "http://plex:32400/status/sessions/history/all",
            params={"X-Plex-Container-Size": "5", "sort": "viewedAt:desc", "apikey": "tok"},
        ),
        json={"MediaContainer": {"Metadata": [{"title": "Dune", "viewedAt": 99, "accountID": 3}]}},
    )
    httpx_mock.add_response(
        url=httpx.URL("http://plex:32400/accounts", params={"apikey": "tok"}),
        json={"MediaContainer": {"Account": [{"id": 1, "name": ""}, {"id": 3, "name": "ana"}]}},
    )
    [entry] = await c.get_history(limit=5)
    assert (entry.viewed_at, entry.account_id) == (99, 3)
    accounts = await c.get_accounts()
    assert [a.display_name for a in accounts] == ["Main User", "ana"]
    await c.close()


async def test_plex_search_hubs(httpx_mock):
    c = PlexClient("http://plex:32400", "tok")
    httpx_mock.add_response(
        json={
            "MediaContainer": {
                "Hub": [{"type": "movie", "Metadata": [{"ratingKey": "5", "title": "Dune"}]}]
            }
        },
    )
    [hub] = await c.search("dune")
    assert hub.metadata[0].rating_key == "5"
    await c.close()


async def test_plex_tv_friend_share_lookup(httpx_mock):
    c = PlexTVClient("tok")
    httpx_mock.add_response(
        url="https://plex.tv/api/v2/friends",
        json=[
            {
                "id": 11,
                "title": "ana",
                "servers": [
                    {
                        "machineIdentifier": "m1",
                        "allLibraries": False,
                        "sections": [{"title": "TV"}],
                    }
                ],
            }
        ],
    )
    [friend] = await c.get_friends()
    share = friend.share_on("m1")
    assert share and [s.title for s in share.sections] == ["TV"]
    assert friend.share_on("other") is None
    await c.close()


async def test_plex_tv_home_users_accepts_wrapped_list(httpx_mock):
    c = PlexTVClient("tok")
    httpx_mock.add_response(
        url="https://plex.tv/api/v2/home/users",
        json={"users": [{"id": 2, "title": "kid", "admin": False}]},
    )
    [user] = await c.get_home_users()
    assert (user.id, user.title, user.admin) == (2, "kid", False)
    await c.close()


async def test_tmdb_movies_and_shows(httpx_mock):
    c = TMDBClient("key")
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.themoviedb.org/3/trending/movie/week",
            params={"language": "en-US", "api_key": "key"},
        ),
        json={
            "results": [
                {
                    "id": 1,
                    "title": "Dune",
                    "media_type": "movie",
                    "release_date": "2021-09-15",
                    "poster_path": "/p.jpg",
                    "vote_average": 7.784,
                }
            ]
        },
    )
    httpx_mock.add_response(
        url=httpx.URL(
            "https://api.themoviedb.org/3/tv/popular",
            params={"language": "en-US", "api_key": "key"},
        ),
        json={"results": [{"id": 2, "name": "Silo", "first_air_date": ""}]},
    )
    [movie] = await c.get_trending_movies()
    assert (movie.display_title, movie.year, movie.rating) == ("Dune", "2021", 7.8)
    assert movie.poster == "https://image.tmdb.org/t/p/w342/p.jpg"
    [show] = await c.get_popular_tv()
    assert (show.media_type, show.display_title, show.year, show.poster) == ("tv", "Silo", "", None)
    await c.close()


async def test_lastfm_top_tracks(httpx_mock):
    c = LastFMClient("key")
    httpx_mock.add_response(
        json={
            "tracks": {
                "track": [
                    {
                        "name": "Song",
                        "listeners": "1234567",
                        "artist": {"name": "Band"},
                        "image": [
                            {"#text": "http://s.png", "size": "small"},
                            {"#text": "http://xl.png", "size": "extralarge"},
                        ],
                    }
                ]
            }
        },
    )
    [card] = await c.get_top_tracks()
    assert (card.display_title, card.artist, card.listeners) == ("Song", "Band", "1.2M")
    assert card.poster == "http://xl.png"
    await c.close()
