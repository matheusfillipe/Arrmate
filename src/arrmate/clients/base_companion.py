"""Base class for companion service API clients.

Companion services supplement primary media services (like Sonarr/Radarr)
rather than managing media directly, for example Bazarr for subtitles.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Any

import httpx


class BaseCompanionClient(ABC):
    """Abstract base class for companion service clients."""

    def __init__(self, base_url: str, api_key: str, timeout: int = 30) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={"X-Api-Key": self.api_key},
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, endpoint: str, params: Mapping[str, int | str] | None = None) -> Any:
        """GET an endpoint and return its raw JSON for the subclass to validate."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    @abstractmethod
    async def test_connection(self) -> bool:
        """Return True when the companion service answers."""
