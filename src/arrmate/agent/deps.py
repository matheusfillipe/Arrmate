"""Shared runtime dependencies passed to every agent tool call."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from arrmate.clients.cleanuparr import CleanuparrClient
from arrmate.clients.jellyfin import JellyfinClient
from arrmate.clients.jellyseerr import JellyseerrClient
from arrmate.clients.prowlarr import ProwlarrClient
from arrmate.clients.qbittorrent import QBittorrentClient
from arrmate.clients.radarr import RadarrClient
from arrmate.clients.sonarr import SonarrClient
from arrmate.config import instances
from arrmate.config.settings import settings

ROLE_USER = "user"
ROLE_POWER_USER = "power_user"
ROLE_ADMIN = "admin"

_WRITE_ROLES = {ROLE_POWER_USER, ROLE_ADMIN}


@dataclass
class AgentDeps:
    """Per-request dependencies: who is asking, and client factories.

    Clients are short-lived (one HTTP client per tool call) so a leaked
    connection can never outlive the request that created it. Sonarr/Radarr
    are addressed by instance id ("" = the env-var primary).
    """

    user_id: str
    username: str
    role: str = ROLE_USER
    thread_id: str | None = None
    available_services: list[str] = field(default_factory=list)

    @property
    def can_write(self) -> bool:
        return self.role in _WRITE_ROLES

    def require_write(self, action: str = "write") -> None:
        """Raise PermissionError unless the role may perform writes.

        Matches the command-page policy: the user role submits requests,
        power_user and admin execute them directly.
        """
        if not self.can_write:
            raise PermissionError(f"action '{action}' requires the power_user or admin role")

    @asynccontextmanager
    async def sonarr(self, instance_id: str = "") -> AsyncIterator[SonarrClient]:
        inst = instances.get_instance(instance_id, "sonarr")
        if not inst:
            raise ValueError("Sonarr is not configured")
        client = SonarrClient(inst["url"], inst["api_key"])
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def radarr(self, instance_id: str = "") -> AsyncIterator[RadarrClient]:
        inst = instances.get_instance(instance_id, "radarr")
        if not inst:
            raise ValueError("Radarr is not configured")
        client = RadarrClient(inst["url"], inst["api_key"])
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def qbittorrent(self) -> AsyncIterator[QBittorrentClient]:
        if not settings.qbittorrent_url:
            raise ValueError("qBittorrent is not configured")
        client = QBittorrentClient(
            settings.qbittorrent_url,
            settings.qbittorrent_username or "",
            settings.qbittorrent_password or "",
        )
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def prowlarr(self) -> AsyncIterator[ProwlarrClient]:
        if not settings.prowlarr_url or not settings.prowlarr_api_key:
            raise ValueError("Prowlarr is not configured")
        client = ProwlarrClient(settings.prowlarr_url, settings.prowlarr_api_key)
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def cleanuparr(self) -> AsyncIterator[CleanuparrClient]:
        if not settings.cleanuparr_url or not settings.cleanuparr_api_key:
            raise ValueError("Cleanuparr is not configured")
        client = CleanuparrClient(settings.cleanuparr_url, settings.cleanuparr_api_key)
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def jellyfin(self) -> AsyncIterator[JellyfinClient]:
        if not settings.jellyfin_url or not settings.jellyfin_api_key:
            raise ValueError("Jellyfin is not configured")
        client = JellyfinClient(settings.jellyfin_url, settings.jellyfin_api_key)
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def jellyseerr(self) -> AsyncIterator[JellyseerrClient]:
        if not settings.jellyseerr_url or not settings.jellyseerr_api_key:
            raise ValueError("Jellyseerr is not configured")
        client = JellyseerrClient(settings.jellyseerr_url, settings.jellyseerr_api_key)
        try:
            yield client
        finally:
            await client.close()
