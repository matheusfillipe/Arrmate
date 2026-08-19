"""Tests for the Listenarr fill-missing playbook helpers."""

from arrmate.agent.playbooks import _rank_releases, _search_queries


def test_edition_parenthetical_is_dropped_before_the_author_variant():
    """The full Audible title is what Listenarr searches and what matches nothing."""
    queries = _search_queries("Red Rising (Part 1 of 2) (Dramatized Adaptation)", "Pierce Brown")
    assert queries[0] == "Red Rising Pierce Brown"
    assert queries[1] == "Red Rising"


def test_author_only_variant_is_offered_when_the_author_is_the_problem():
    queries = _search_queries("Learn German for Beginners", "Anthony Becker")
    assert queries == ["Learn German for Beginners Anthony Becker", "Learn German for Beginners"]


def test_no_duplicate_queries_when_title_is_already_clean():
    assert _search_queries("Dune", "") == ["Dune"]


def test_clips_below_audiobook_size_are_discarded():
    """Language-learning queries return lesson clips of a few MB."""
    results = [
        {
            "title": "Learn German Numbers Part 1",
            "size": 13_000_000,
            "seeders": 9,
            "downloadReference": "a",
        },
    ]
    assert _rank_releases(results, "Learn German for Beginners") == []


def test_releases_missing_the_title_words_are_discarded():
    results = [
        {
            "title": "Some Other Audiobook",
            "size": 900_000_000,
            "seeders": 99,
            "downloadReference": "a",
        },
    ]
    assert _rank_releases(results, "Red Rising (Part 1 of 2)") == []


def test_ungrabbable_releases_are_discarded():
    """Without a downloadReference there is nothing to send to the client."""
    results = [{"title": "Red Rising", "size": 900_000_000, "seeders": 5}]
    assert _rank_releases(results, "Red Rising") == []


def test_best_seeded_matching_release_wins():
    results = [
        {"title": "Red Rising [MP3]", "size": 800_000_000, "seeders": 3, "downloadReference": "a"},
        {"title": "Red Rising [M4B]", "size": 900_000_000, "seeders": 40, "downloadReference": "b"},
        {"title": "Dark Age", "size": 630_000_000, "seeders": 99, "downloadReference": "c"},
    ]
    ranked = _rank_releases(results, "Red Rising (Dramatized Adaptation)")
    assert [r["downloadReference"] for r in ranked] == ["b", "a"]
