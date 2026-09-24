"""Persistence layer for service settings (saved to /data/services.json).

Settings priority:
  1. Environment variables (set at container launch, always win at startup)
  2. /data/services.json (written by the Settings UI, applied at startup for
     fields that env vars left empty)
  3. Pydantic field defaults

When the UI saves a value it is applied to the running settings object immediately
(no restart needed) AND written to services.json so it survives restarts.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path

import pydantic
from pydantic import JsonValue, TypeAdapter

from arrmate.config.settings import settings

logger = logging.getLogger(__name__)

# Every field that can be edited through the Settings UI
CONFIGURABLE_FIELDS: set[str] = {
    # LLM
    "llm_provider",
    "ollama_base_url",
    "ollama_model",
    "openai_api_key",
    "openai_model",
    "openai_base_url",
    "anthropic_api_key",
    "anthropic_model",
    # Media services
    "sonarr_url",
    "sonarr_api_key",
    "radarr_url",
    "radarr_api_key",
    "lidarr_url",
    "lidarr_api_key",
    "bazarr_url",
    "bazarr_api_key",
    "plex_url",
    "plex_token",
    "audiobookshelf_url",
    "audiobookshelf_api_key",
    "lazylibrarian_url",
    "lazylibrarian_api_key",
    "readmeabook_url",
    "readmeabook_api_key",
    # External APIs
    "tmdb_api_key",
    # Download clients
    "sabnzbd_url",
    "sabnzbd_api_key",
    "nzbget_url",
    "nzbget_username",
    "nzbget_password",
    "qbittorrent_url",
    "qbittorrent_username",
    "qbittorrent_password",
    "transmission_url",
    "transmission_username",
    "transmission_password",
    # Cleanuparr (experimental)
    "cleanuparr_url",
    "cleanuparr_api_key",
    # Jellyfin / Jellyseerr
    "jellyfin_url",
    "jellyfin_api_key",
    "jellyseerr_url",
    "jellyseerr_api_key",
    # Notification webhooks
    "slack_webhook_url",
    "discord_webhook_url",
    # General
    "arrmate_base_url",
    # Plex SSO
    "plex_sso_enabled",
    "plex_sso_default_role",
    "plex_sso_require_approval",
    "plex_sso_verify_plex_friends",
}


_BOOL_FIELDS = {"plex_sso_enabled", "plex_sso_require_approval", "plex_sso_verify_plex_friends"}

ConfigValue = str | bool | None

#: services.json holds the configurable fields plus `media_instances`, so it stays plain JSON.
_SAVED_CONFIG = TypeAdapter(dict[str, JsonValue])


def _config_path() -> Path:
    return Path(settings.auth_data_dir) / "services.json"


def load_saved_config() -> dict[str, JsonValue]:
    path = _config_path()
    if path.exists():
        try:
            return _SAVED_CONFIG.validate_json(path.read_text())
        except (pydantic.ValidationError, OSError) as e:
            logger.warning("Could not read services.json: %s", e)
    return {}


def apply_saved_config() -> None:
    """Apply services.json to settings for fields that env vars left empty.

    Call once at startup, after Pydantic has already loaded env vars.
    """

    saved = load_saved_config()
    for key, value in saved.items():
        if key not in CONFIGURABLE_FIELDS or not hasattr(settings, key):
            continue
        # Only fill in if env var left the field empty / None
        if not getattr(settings, key, None) and value:
            try:
                setattr(settings, key, value)
            except pydantic.ValidationError:
                logger.debug("setting %s rejected update", key)


def _normalize(key: str, value: str) -> ConfigValue:
    """Checkboxes post "on" when checked; an empty text field clears its setting."""
    if key in _BOOL_FIELDS:
        return value in ("on", "true", "1")
    return value.strip() or None


def save_service_config(updates: Mapping[str, str]) -> None:
    """Persist form values to services.json and apply them to settings in memory.

    A checkbox missing from the form is unchecked, so its setting is cleared.
    """

    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = load_saved_config()

    for key, value in updates.items():
        if key not in CONFIGURABLE_FIELDS:
            continue
        normalized = _normalize(key, value)
        existing[key] = normalized
        if hasattr(settings, key):
            try:
                setattr(settings, key, normalized)
            except pydantic.ValidationError:
                logger.debug("setting %s rejected update", key)

    for key in _BOOL_FIELDS:
        if key in CONFIGURABLE_FIELDS and key not in updates:
            existing[key] = False
            if hasattr(settings, key):
                try:
                    setattr(settings, key, False)
                except pydantic.ValidationError:
                    logger.debug("setting %s rejected update", key)

    path.write_bytes(_SAVED_CONFIG.dump_json(existing, indent=2))
    os.chmod(path, 0o600)


def get_service_config() -> dict[str, ConfigValue]:
    """Return current settings values for all configurable fields."""

    return {field: getattr(settings, field, None) for field in CONFIGURABLE_FIELDS}
