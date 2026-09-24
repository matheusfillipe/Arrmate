"""Base class for media service API clients."""

from abc import ABC, abstractmethod
from typing import Any

import httpx


class BaseMediaClient(ABC):
    """HTTP transport shared by the media service clients.

    The raw JSON helpers return ``Any``; each client validates it into its own models
    before anything leaves the client.
    """

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

    async def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def _get_with_timeout(
        self, endpoint: str, params: dict[str, Any] | None = None, timeout: float = 180.0
    ) -> Any:
        """GET with an extended per-request timeout.

        Interactive release searches query live indexers and can take 30-180s;
        they must not inherit the client-wide short timeout or the agent will
        conclude the service is broken and retry, doubling indexer load.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()

    async def _post(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.post(url, json=data)
        response.raise_for_status()
        return response.json()

    async def _put(self, endpoint: str, data: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.put(url, json=data)
        response.raise_for_status()
        return response.json() if response.text else None

    async def _delete(self, endpoint: str) -> Any:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        response = await self.client.delete(url)
        response.raise_for_status()
        return response.json() if response.text else None

    @abstractmethod
    async def test_connection(self) -> bool:
        """True when the service answers."""
