"""Tests for the multi-instance registry (step 5)."""

import pytest

from arrmate.config import instances


@pytest.fixture(autouse=True)
def patch_sources(monkeypatch, tmp_path):
    """Primary from settings-like stub; extras from a services.json file."""
    import arrmate.config.instances as mod

    monkeypatch.setattr(
        mod.settings.__class__,
        "sonarr_url",
        property(lambda self: "http://sonarr:8989"),
        raising=False,
    )
    monkeypatch.setattr(
        mod.settings.__class__,
        "sonarr_api_key",
        property(lambda self: "k1"),
        raising=False,
    )
    monkeypatch.setattr(
        mod.settings.__class__,
        "radarr_url",
        property(lambda self: None),
        raising=False,
    )
    monkeypatch.setattr(
        mod.settings.__class__,
        "radarr_api_key",
        property(lambda self: None),
        raising=False,
    )

    def fake_load():
        import json

        return json.loads((tmp_path / "services.json").read_text())

    import arrmate.config.service_config as sc

    monkeypatch.setattr(sc, "_load_json", lambda: fake_load())
    yield


def _write_extras(tmp_path, extras):
    import json

    (tmp_path / "services.json").write_text(json.dumps({"media_instances": extras}))


def test_primary_only(tmp_path):
    _write_extras(tmp_path, [])
    lst = instances.list_instances()
    assert [i["id"] for i in lst] == ["sonarr"]


def test_extra_instance_resolves(tmp_path):
    _write_extras(
        tmp_path,
        [{"id": "sonarr-4k", "type": "sonarr", "url": "http://sonarr4k:8989", "api_key": "k2"}],
    )
    inst = instances.get_instance("sonarr-4k", "sonarr")
    assert inst["url"] == "http://sonarr4k:8989"
    assert inst["api_key"] == "k2"


def test_empty_id_falls_back_to_primary(tmp_path):
    _write_extras(
        tmp_path,
        [{"id": "sonarr-4k", "type": "sonarr", "url": "http://sonarr4k:8989", "api_key": "k2"}],
    )
    inst = instances.get_instance("", "sonarr")
    assert inst["id"] == "sonarr"
    assert inst["url"] == "http://sonarr:8989"


def test_unknown_id_returns_none(tmp_path):
    _write_extras(tmp_path, [])
    assert instances.get_instance("sonarr-4k", "sonarr") is None


def test_invalid_entries_dropped(tmp_path):
    _write_extras(
        tmp_path,
        [
            {"id": "", "type": "sonarr", "url": "http://x"},
            {"id": "ok", "type": "bogus", "url": "http://x"},
            {"id": "nourl", "type": "sonarr"},
            {"id": "sonarr-4k", "type": "sonarr", "url": "http://sonarr4k:8989", "api_key": "k2"},
        ],
    )
    lst = instances.list_instances()
    ids = [i["id"] for i in lst]
    assert ids == ["sonarr", "sonarr-4k"]
