"""Named multi-instance service registry.

The env-var service (SONARR_URL etc.) is always the primary instance, id
``sonarr`` / ``radarr``. Extra instances (e.g. a 4K Sonarr) live in
services.json under ``media_instances`` as a list of
``{"id": "sonarr-4k", "type": "sonarr", "url": ..., "api_key": ...}``.
"""

import logging

from .settings import settings

logger = logging.getLogger(__name__)

VALID_TYPES = ("sonarr", "radarr")


def _load_instances() -> list[dict]:
    """Read extra instances from services.json (invalid entries dropped)."""
    from .service_config import _load_json

    raw = _load_json().get("media_instances") or []
    out = []
    for entry in raw:
        if (
            isinstance(entry, dict)
            and entry.get("id")
            and entry.get("type") in VALID_TYPES
            and entry.get("url")
        ):
            out.append(
                {
                    "id": str(entry["id"]),
                    "type": entry["type"],
                    "url": entry["url"],
                    "api_key": entry.get("api_key") or "",
                }
            )
    return out


def list_instances() -> list[dict]:
    """All addressable instances: the env primary first, then extras."""
    primary = []
    if settings.sonarr_url and settings.sonarr_api_key:
        primary.append(
            {
                "id": "sonarr",
                "type": "sonarr",
                "url": settings.sonarr_url,
                "api_key": settings.sonarr_api_key,
            }
        )
    if settings.radarr_url and settings.radarr_api_key:
        primary.append(
            {
                "id": "radarr",
                "type": "radarr",
                "url": settings.radarr_url,
                "api_key": settings.radarr_api_key,
            }
        )
    seen = {p["id"] for p in primary}
    extras = [i for i in _load_instances() if i["id"] not in seen]
    return primary + extras


def get_instance(instance_id: str | None, instance_type: str) -> dict | None:
    """Resolve an instance by id, falling back to the primary of that type.

    instance_id empty or equal to the primary name returns the env primary.
    Returns {"id", "url", "api_key"} or None when nothing is configured.
    """
    primary_id = instance_type  # "sonarr"/"radarr"
    if not instance_id or instance_id == primary_id:
        url = getattr(settings, f"{instance_type}_url")
        key = getattr(settings, f"{instance_type}_api_key")
        if url and key:
            return {"id": primary_id, "url": url, "api_key": key}
        return None
    for inst in _load_instances():
        if inst["id"] == instance_id and inst["type"] == instance_type:
            return inst
    return None
