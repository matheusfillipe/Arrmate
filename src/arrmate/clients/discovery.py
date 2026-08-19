"""Service discovery for media clients."""

import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from arrmate.config.settings import settings
from arrmate.core.models import EnhancedServiceInfo, ImplementationStatus, ServiceCapability

from .audiobookshelf import AudioBookshelfClient
from .base_arr import BaseArrClient
from .bazarr import BazarrClient
from .cleanuparr import CleanuparrClient
from .gamearr import GamearrClient
from .jellyfin import JellyfinClient
from .jellyseerr import JellyseerrClient
from .lazylibrarian import LazyLibrarianClient
from .lidarr import LidarrClient
from .listenarr import ListenarrClient
from .plex import PlexClient
from .prowlarr import ProwlarrClient
from .radarr import RadarrClient
from .readarr import ReadarrClient
from .readmeabook import ReadMeABookClient
from .sonarr import SonarrClient

logger = logging.getLogger(__name__)

DEFAULT_PORTS = {
    "sonarr": 8989,
    "radarr": 7878,
    "lidarr": 8686,
    "readarr": 8787,
    "bazarr": 6767,
    "audiobookshelf": 13378,
    "lazylibrarian": 5299,
    "listenarr": 4545,
    "readmeabook": 3030,
    "plex": 32400,
    "prowlarr": 9696,
}

_FULL = ServiceCapability(
    can_search=True, can_add=True, can_remove=True, can_upgrade=True, can_list=True
)
_MANAGE_LIBRARY = ServiceCapability(
    can_search=True, can_add=True, can_remove=True, can_upgrade=False, can_list=True
)
_SUBTITLES = ServiceCapability(
    can_search=True, can_add=False, can_remove=False, can_upgrade=False, can_list=True
)
_MEDIA_SERVER = ServiceCapability(
    can_search=True, can_add=False, can_remove=True, can_upgrade=False, can_list=True
)
_UPLOAD_ONLY = ServiceCapability(
    can_search=True, can_add=False, can_remove=True, can_upgrade=False, can_list=True
)
_REQUESTS = ServiceCapability(
    can_search=True, can_add=True, can_remove=False, can_upgrade=False, can_list=True
)
_READ_ONLY = ServiceCapability(
    can_search=True, can_add=False, can_remove=False, can_upgrade=False, can_list=True
)
_CLEANUP = ServiceCapability(
    can_search=False, can_add=False, can_remove=True, can_upgrade=False, can_list=True
)

VersionFn = Callable[[Any], Awaitable[str | None]]


async def _system_status_version(client: Any) -> str | None:
    status = await client.get_system_status()
    version: str | None = status.get("version")
    return version


async def _bazarr_version(client: BazarrClient) -> str | None:
    status = await client.get_system_status()
    version: str | None = status.get("data", {}).get("bazarr_version")
    return version


async def _audiobookshelf_version(client: AudioBookshelfClient) -> str | None:
    status = await client.get_system_status()
    version: str | None = status.get("serverVersion") or status.get("version")
    return version


async def _client_get_version(client: Any) -> str | None:
    version = await client.get_version()
    return str(version) if version is not None else None


async def _jellyfin_version(client: JellyfinClient) -> str | None:
    info = await client.get_system_info()
    version: str | None = info.get("Version")
    return version


@dataclass(frozen=True)
class ServiceSpec:
    """Static description plus probe wiring for one service."""

    name: str
    url_attr: str
    key_attr: str
    client_cls: type
    status: ImplementationStatus
    api_version: str
    media_type: str
    capabilities: ServiceCapability
    version_fn: VersionFn | None = None
    #: True when the service answers unauthenticated requests, so a URL alone configures it.
    key_optional: bool = False
    is_deprecated: bool = False
    deprecation_message: str | None = None


SERVICE_REGISTRY: dict[str, ServiceSpec] = {
    spec.name: spec
    for spec in (
        ServiceSpec(
            name="sonarr",
            url_attr="sonarr_url",
            key_attr="sonarr_api_key",
            client_cls=SonarrClient,
            status=ImplementationStatus.COMPLETE,
            api_version="v3",
            media_type="TV Shows",
            capabilities=_FULL,
            version_fn=_system_status_version,
        ),
        ServiceSpec(
            name="radarr",
            url_attr="radarr_url",
            key_attr="radarr_api_key",
            client_cls=RadarrClient,
            status=ImplementationStatus.COMPLETE,
            api_version="v3",
            media_type="Movies",
            capabilities=_FULL,
            version_fn=_system_status_version,
        ),
        ServiceSpec(
            name="lidarr",
            url_attr="lidarr_url",
            key_attr="lidarr_api_key",
            client_cls=LidarrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="v3",
            media_type="Music",
            capabilities=_MANAGE_LIBRARY,
            version_fn=_system_status_version,
        ),
        ServiceSpec(
            name="readarr",
            url_attr="readarr_url",
            key_attr="readarr_api_key",
            client_cls=ReadarrClient,
            status=ImplementationStatus.DEPRECATED,
            api_version="v1",
            media_type="Books/Audiobooks",
            capabilities=_MANAGE_LIBRARY,
            version_fn=_system_status_version,
            is_deprecated=True,
            deprecation_message=(
                "Readarr project is retired. Support limited to existing instances."
            ),
        ),
        ServiceSpec(
            name="bazarr",
            url_attr="bazarr_url",
            key_attr="bazarr_api_key",
            client_cls=BazarrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="custom",
            media_type="Subtitles",
            capabilities=_SUBTITLES,
            version_fn=_bazarr_version,
        ),
        ServiceSpec(
            name="audiobookshelf",
            url_attr="audiobookshelf_url",
            key_attr="audiobookshelf_api_key",
            client_cls=AudioBookshelfClient,
            status=ImplementationStatus.PARTIAL,
            api_version="REST",
            media_type="Audiobooks/Podcasts",
            capabilities=_UPLOAD_ONLY,
            version_fn=_audiobookshelf_version,
        ),
        ServiceSpec(
            name="lazylibrarian",
            url_attr="lazylibrarian_url",
            key_attr="lazylibrarian_api_key",
            client_cls=LazyLibrarianClient,
            status=ImplementationStatus.PARTIAL,
            api_version="custom",
            media_type="Books/Audiobooks",
            capabilities=_MANAGE_LIBRARY,
            version_fn=_system_status_version,
        ),
        ServiceSpec(
            name="readmeabook",
            url_attr="readmeabook_url",
            key_attr="readmeabook_api_key",
            client_cls=ReadMeABookClient,
            status=ImplementationStatus.PARTIAL,
            api_version="REST",
            media_type="Audiobooks",
            capabilities=_REQUESTS,
            version_fn=_client_get_version,
        ),
        ServiceSpec(
            name="plex",
            url_attr="plex_url",
            key_attr="plex_token",
            client_cls=PlexClient,
            status=ImplementationStatus.PARTIAL,
            api_version="REST",
            media_type="Media Server",
            capabilities=_MEDIA_SERVER,
            version_fn=_client_get_version,
        ),
        ServiceSpec(
            name="prowlarr",
            url_attr="prowlarr_url",
            key_attr="prowlarr_api_key",
            client_cls=ProwlarrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="v1",
            media_type="Indexer Aggregator",
            capabilities=_READ_ONLY,
            version_fn=_system_status_version,
        ),
        ServiceSpec(
            name="cleanuparr",
            url_attr="cleanuparr_url",
            key_attr="cleanuparr_api_key",
            client_cls=CleanuparrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="reverse-engineered",
            media_type="Download Cleanup",
            capabilities=_CLEANUP,
        ),
        ServiceSpec(
            name="jellyfin",
            url_attr="jellyfin_url",
            key_attr="jellyfin_api_key",
            client_cls=JellyfinClient,
            status=ImplementationStatus.PARTIAL,
            api_version="stable",
            media_type="Media Server",
            capabilities=_MEDIA_SERVER,
            version_fn=_jellyfin_version,
        ),
        ServiceSpec(
            name="jellyseerr",
            url_attr="jellyseerr_url",
            key_attr="jellyseerr_api_key",
            client_cls=JellyseerrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="v1",
            media_type="Request Management",
            capabilities=_REQUESTS,
        ),
        ServiceSpec(
            name="listenarr",
            url_attr="listenarr_url",
            key_attr="listenarr_api_key",
            client_cls=ListenarrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="v1",
            media_type="Audiobooks",
            capabilities=_MANAGE_LIBRARY,
            version_fn=_system_status_version,
        ),
        ServiceSpec(
            name="gamearr",
            url_attr="gamearr_url",
            key_attr="gamearr_api_key",
            client_cls=GamearrClient,
            status=ImplementationStatus.PARTIAL,
            api_version="v1",
            media_type="Games",
            capabilities=_MANAGE_LIBRARY,
            # Gamearr only demands a key once an admin account exists; until then it serves
            # the API unauthenticated and a URL on its own is a working configuration.
            key_optional=True,
        ),
    )
}


def _mask_api_key(api_key: str | None) -> str | None:
    """Mask an API key for display, showing only the last 4 characters."""
    if not api_key or len(api_key) < 4:
        return None
    return "***" + api_key[-4:]


def _info(spec: ServiceSpec, available: bool, version: str | None) -> EnhancedServiceInfo:
    return EnhancedServiceInfo(
        name=spec.name,
        url=str(getattr(settings, spec.url_attr)),
        api_key=_mask_api_key(getattr(settings, spec.key_attr)),
        available=available,
        version=version,
        implementation_status=spec.status,
        api_version=spec.api_version,
        capabilities=spec.capabilities,
        media_type=spec.media_type,
        is_deprecated=spec.is_deprecated,
        deprecation_message=spec.deprecation_message,
    )


async def _probe(spec: ServiceSpec) -> EnhancedServiceInfo:
    """Probe one service and build its info card."""
    try:
        client = spec.client_cls(
            str(getattr(settings, spec.url_attr)),
            str(getattr(settings, spec.key_attr)),
        )
        try:
            available = bool(await client.test_connection())
            version = None
            if available and spec.version_fn is not None:
                try:
                    version = await spec.version_fn(client)
                except (httpx.HTTPError, KeyError, ValueError):
                    logger.debug("version probe failed for %s", spec.name, exc_info=True)
        finally:
            with contextlib.suppress(httpx.HTTPError):
                await client.close()
        return _info(spec, available, version)
    except (httpx.HTTPError, ValueError) as e:
        logger.error("Error discovering %s: %s", spec.name, e)
        return _info(spec, available=False, version=None)


def _is_configured(spec: ServiceSpec) -> bool:
    if not getattr(settings, spec.url_attr):
        return False
    return spec.key_optional or bool(getattr(settings, spec.key_attr))


async def discover_services() -> dict[str, EnhancedServiceInfo]:
    """Probe every configured service and report availability, version, and metadata."""
    services: dict[str, EnhancedServiceInfo] = {}
    for spec in SERVICE_REGISTRY.values():
        if _is_configured(spec):
            services[spec.name] = await _probe(spec)
    return services


def get_client_for_media_type(media_type: str) -> BaseArrClient:
    """Get the primary client for a media type ('tv', 'movie', 'music', 'audiobook', 'book')."""
    spec = {
        "tv": "sonarr",
        "movie": "radarr",
        "music": "lidarr",
        "audiobook": "readarr",
        "book": "readarr",
    }.get(media_type)
    if spec is None:
        raise ValueError(f"Unsupported media type: {media_type}")

    entry = SERVICE_REGISTRY[spec]
    url = getattr(settings, entry.url_attr)
    key = getattr(settings, entry.key_attr)
    if not _is_configured(entry):
        raise ValueError(
            f"{spec.capitalize()} is not configured. "
            f"Set {spec.upper()}_URL and {spec.upper()}_API_KEY."
        )
    if entry.is_deprecated:
        logger.warning(
            "Using deprecated %s client. Project is retired. "
            "Consider alternatives like Calibre-Web or LazyLibrarian.",
            spec.capitalize(),
        )
    client: BaseArrClient = entry.client_cls(str(url), str(key))
    return client
