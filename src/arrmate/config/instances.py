"""Named multi-instance service registry.

The env-var service (SONARR_URL etc.) is always the primary instance, id
``sonarr`` / ``radarr``. Extra instances (e.g. a 4K Sonarr) live in
services.json under ``media_instances`` as a list of
``{"id": "sonarr-4k", "type": "sonarr", "url": ..., "api_key": ...}``.
"""

import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from . import service_config
from .settings import settings

logger = logging.getLogger(__name__)

InstanceType = Literal["sonarr", "radarr"]


class MediaInstance(BaseModel):
    id: str = Field(min_length=1)
    type: InstanceType
    url: str = Field(min_length=1)
    api_key: str | None = None


def _load_instances() -> list[MediaInstance]:
    """Read extra instances from services.json (invalid entries dropped)."""
    raw = service_config.load_saved_config().get("media_instances")
    instances = []
    for entry in raw if isinstance(raw, list) else []:
        try:
            instances.append(MediaInstance.model_validate(entry))
        except ValidationError:
            logger.warning("ignoring invalid media instance in services.json: %r", entry)
    return instances


def _primary(instance_type: InstanceType) -> MediaInstance | None:
    if instance_type == "sonarr":
        url, key = settings.sonarr_url, settings.sonarr_api_key
    else:
        url, key = settings.radarr_url, settings.radarr_api_key
    if url and key:
        return MediaInstance(id=instance_type, type=instance_type, url=url, api_key=key)
    return None


def list_instances() -> list[MediaInstance]:
    """All addressable instances: the env primary first, then extras."""
    primary = [inst for inst in (_primary("sonarr"), _primary("radarr")) if inst]
    seen = {p.id for p in primary}
    return primary + [i for i in _load_instances() if i.id not in seen]


def get_instance(instance_id: str | None, instance_type: InstanceType) -> MediaInstance | None:
    """Resolve an instance by id, falling back to the primary of that type.

    instance_id empty or equal to the primary name returns the env primary.
    Returns None when nothing is configured.
    """
    if not instance_id or instance_id == instance_type:
        return _primary(instance_type)
    for inst in _load_instances():
        if inst.id == instance_id and inst.type == instance_type:
            return inst
    return None
