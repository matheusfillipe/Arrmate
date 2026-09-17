"""Tests for resolving requested songs to Lidarr albums."""

from typing import Any

import pytest

from arrmate.agent.music_tools import (
    best_song,
    build_playlist,
    ensure_tracks,
    matching_tracks,
    parse_track_line,
    preferred_album,
)


class FakeLidarr:
    """Just enough of Lidarr's library for the song resolution to run against."""

    def __init__(self) -> None:
        self.artists = [{"id": 1, "artistName": "Scorpions"}]
        self.albums = {
            1: [
                {
                    "id": 10,
                    "title": "Love at First Sting",
                    "albumType": "Album",
                    "secondaryTypes": [],
                    "releaseDate": "1984-03-27",
                    "monitored": True,
                },
                {
                    "id": 11,
                    "title": "Moment of Glory",
                    "albumType": "Album",
                    "secondaryTypes": ["Live"],
                    "releaseDate": "2000-01-01",
                    "monitored": False,
                },
                {
                    "id": 12,
                    "title": "Blackout",
                    "albumType": "Album",
                    "secondaryTypes": [],
                    "releaseDate": "1982-03-29",
                    "monitored": False,
                },
                {
                    "id": 13,
                    "title": "Lovedrive",
                    "albumType": "Album",
                    "secondaryTypes": [],
                    "releaseDate": "1979-02-25",
                    "monitored": False,
                },
            ]
        }
        self.tracks = {
            1: [
                {"title": "Still Loving You", "albumId": 10, "hasFile": True},
                {"title": "Still Loving You", "albumId": 11, "hasFile": False},
                {
                    "title": "When the Smoke Is Going Down (2015 Remaster)",
                    "albumId": 12,
                    "hasFile": False,
                },
                {"title": "Holiday", "albumId": 13, "hasFile": False},
            ]
        }
        self.queue: list[dict[str, Any]] = [{"albumId": 13, "trackedDownloadState": "downloading"}]
        self.monitored: list[int] = []
        self.searched: list[int] = []

    async def get_all_items(self) -> list[dict[str, Any]]:
        return self.artists

    async def get_queue(self) -> dict[str, Any]:
        return {"records": self.queue}

    async def get_artist_tracks(self, artist_id: int) -> list[dict[str, Any]]:
        return self.tracks[artist_id]

    async def get_albums(self, artist_id: int) -> list[dict[str, Any]]:
        return self.albums[artist_id]

    async def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> None:
        self.monitored.extend(album_ids)

    async def trigger_album_search(self, album_ids: list[int]) -> None:
        self.searched.extend(album_ids)


def test_track_line_splits_at_the_first_separator():
    assert parse_track_line("Cutting Crew - (I Just) Died In Your Arms") == (
        "Cutting Crew",
        "(I Just) Died In Your Arms",
    )


def test_track_line_without_a_title_is_refused():
    with pytest.raises(ValueError):
        parse_track_line("Alphaville")


def test_remaster_suffix_still_matches_and_exact_spelling_ranks_first():
    tracks = [
        {"title": "La isla bonita (extended remix)"},
        {"title": "La Isla Bonita"},
        {"title": "Isla"},
    ]
    assert [t["title"] for t in matching_tracks(tracks, "La Isla Bonita")] == [
        "La Isla Bonita",
        "La isla bonita (extended remix)",
    ]


def test_original_studio_album_beats_live_and_compilation():
    albums = [
        {"id": 1, "albumType": "Compilation", "secondaryTypes": [], "releaseDate": "1980"},
        {"id": 2, "albumType": "Album", "secondaryTypes": ["Live"], "releaseDate": "1979"},
        {"id": 3, "albumType": "Album", "secondaryTypes": [], "releaseDate": "1990"},
        {"id": 4, "albumType": "Album", "secondaryTypes": [], "releaseDate": "1984"},
    ]
    assert preferred_album(albums)["id"] == 4


async def test_owned_queued_missing_and_unknown_songs_are_told_apart():
    lidarr = FakeLidarr()
    report = await ensure_tracks(
        lidarr,  # type: ignore[arg-type]
        [
            "Scorpions - Still Loving You",
            "Scorpions - When The Smoke Is Going Down",
            "Scorpions - Holiday",
            "Scorpions - Wind of Change",
        ],
        can_write=True,
    )
    assert [r["status"] for r in report] == [
        "have",
        "searching",
        "already-queued",
        "song-not-in-lidarr",
    ]
    assert lidarr.monitored == [12]
    assert lidarr.searched == [12]


async def test_without_write_access_nothing_changes():
    lidarr = FakeLidarr()
    report = await ensure_tracks(
        lidarr,  # type: ignore[arg-type]
        ["Scorpions - When The Smoke Is Going Down", "Alphaville - Forever Young"],
        can_write=False,
    )
    assert [r["status"] for r in report] == ["would-search", "would-add-artist"]
    assert lidarr.monitored == [] and lidarr.searched == []


class FakeNavidrome:
    """A music server with a few songs and one existing playlist."""

    username = "service"

    def __init__(self) -> None:
        self.songs = [
            {
                "id": "live",
                "title": "Still Loving You",
                "artist": "Scorpions & Berliner Philharmoniker",
            },
            {"id": "studio", "title": "Still Loving You", "artist": "Scorpions"},
            {"id": "wings", "title": "Broken Wings", "artist": "Mr. Mister"},
        ]
        self.playlists = [{"id": "p1", "name": "80s Ballads"}]
        self.tracks = {"p1": [{"mediaFileId": "wings"}]}
        self.updates: list[tuple[str, bool, str]] = []

    async def search_songs(self, title: str) -> list[dict[str, Any]]:
        return [s for s in self.songs if title.casefold() in s["title"].casefold()]

    async def get_playlists(self) -> list[dict[str, Any]]:
        return self.playlists

    async def get_playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        return self.tracks.setdefault(playlist_id, [])

    async def create_playlist(self, name: str) -> str:
        self.playlists.append({"id": "p2", "name": name})
        return "p2"

    async def add_to_playlist(self, playlist_id: str, song_ids: list[str]) -> int:
        self.tracks[playlist_id].extend({"mediaFileId": i} for i in song_ids)
        return len(song_ids)

    async def update_playlist(
        self, playlist_id: str, name: str, public: bool, owner_id: str
    ) -> None:
        self.updates.append((playlist_id, public, owner_id))

    async def get_user_id(self, username: str) -> str:
        return f"id-{username}"


def test_the_artist_itself_beats_a_collaboration_it_is_part_of():
    song = best_song(FakeNavidrome().songs, "Scorpions", "Still Loving You")
    assert song is not None and song["id"] == "studio"


def test_a_same_titled_song_by_someone_else_is_not_a_match():
    assert best_song(FakeNavidrome().songs, "Alphaville", "Broken Wings") is None


async def test_existing_playlist_gains_only_new_songs_and_goes_to_the_owner():
    navidrome = FakeNavidrome()
    result = await build_playlist(
        navidrome,  # type: ignore[arg-type]
        "80s ballads",
        [
            "Mr. Mister - Broken Wings",
            "Scorpions - Still Loving You",
            "Berlin - Take My Breath Away",
        ],
        public=True,
        owner="mattf",
    )
    assert result["created"] is False
    assert result["added"] == 1 and result["alreadyInPlaylist"] == 1
    assert result["notInNavidrome"] == ["Berlin - Take My Breath Away"]
    assert [t["mediaFileId"] for t in navidrome.tracks["p1"]] == ["wings", "studio"]
    assert navidrome.updates == [("p1", True, "id-mattf")]
