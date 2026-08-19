"""Listenarr audiobook manager client.

Auth: ``X-Api-Key`` header, base ``/api/v1``. Responses are plain JSON, so the
shared :class:`BaseMediaClient` transport applies unchanged. The routes are
*arr-adjacent but not *arr-shaped: the library noun is ``library`` rather than a
media-specific one, there is no ``/command`` queue, and indexer search is a
first-class ``/search`` endpoint instead of a per-item trigger.
"""

import logging
from typing import Any, cast

import httpx

from .base import BaseMediaClient

logger = logging.getLogger(__name__)


class ListenarrClient(BaseMediaClient):
    """Client for the Listenarr v1 API (Audiobooks)."""

    api_prefix = "api/v1"

    async def test_connection(self) -> bool:
        """True when the system info endpoint answers with valid JSON."""
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> dict[str, Any]:
        """Version and runtime info.

        ``/system/status`` is not a route here; an unknown path falls through to
        the SPA and returns HTML, so this has to be ``/system/info``.
        """
        status = await self._get(f"{self.api_prefix}/system/info")
        return cast("dict[str, Any]", status)

    async def get_ready(self) -> dict[str, Any]:
        """Readiness detail: database, migrations and filesystem state."""
        ready = await self._get(f"{self.api_prefix}/system/ready")
        return cast("dict[str, Any]", ready)

    async def get_health(self) -> dict[str, Any]:
        """Health of the configured indexers and download clients."""
        health = await self._get(f"{self.api_prefix}/system/health")
        return cast("dict[str, Any]", health)

    async def get_all_items(self) -> list[dict[str, Any]]:
        """Every audiobook in the library."""
        items = await self._get(f"{self.api_prefix}/library")
        return cast("list[dict[str, Any]]", items)

    async def get_item(self, item_id: int) -> dict[str, Any]:
        """One audiobook by library ID."""
        item = await self._get(f"{self.api_prefix}/library/{item_id}")
        return cast("dict[str, Any]", item)

    async def delete_item(self, item_id: int, delete_files: bool = False) -> bool:
        """Remove an audiobook, optionally deleting its files."""
        await self._delete(
            f"{self.api_prefix}/library/{item_id}?deleteFiles={str(delete_files).lower()}"
        )
        return True

    async def search(
        self, query: str, category: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Search the configured indexers for releases.

        This hits indexers directly, unlike the *arr ``lookup`` endpoints which
        search a metadata catalogue, so it runs on the extended timeout: a search
        fanned out over every configured indexer routinely outlives the default.
        """
        params: dict[str, Any] = {"query": query}
        if category:
            params["category"] = category
        results = await self._get_with_timeout(f"{self.api_prefix}/search", params=params)
        if isinstance(results, dict):
            results = results.get("indexerResults") or results.get("results") or []
        return cast("list[dict[str, Any]]", results)[:limit]

    async def search_metadata(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Search Audible/metadata providers for books, for the add workflow."""
        results = await self._get_with_timeout(
            f"{self.api_prefix}/search/intelligent",
            params={"query": query, "returnLimit": limit},
        )
        return cast("list[dict[str, Any]]", results)

    async def add_book(
        self,
        metadata: dict[str, Any],
        quality_profile_id: int | None = None,
        monitored: bool = True,
        auto_search: bool = False,
    ) -> dict[str, Any]:
        """Add a book to the library.

        ``metadata`` is one result from :meth:`search_metadata`, passed through as
        the Audible metadata object the add request wraps.
        """
        payload: dict[str, Any] = {
            "metadata": metadata,
            "monitored": monitored,
            "autoSearch": auto_search,
        }
        if quality_profile_id is not None:
            payload["qualityProfileId"] = quality_profile_id
        added = await self._post(f"{self.api_prefix}/library/add", data=payload)
        return cast("dict[str, Any]", added)

    async def grab_release(
        self,
        download_reference: str,
        download_client_id: str | None = None,
        audiobook_id: int | None = None,
    ) -> Any:
        """Send one release to a download client.

        The release is identified by the ``downloadReference`` string carried on
        each search result, not by the result object itself.
        """
        payload: dict[str, Any] = {"downloadReference": download_reference}
        if download_client_id:
            payload["downloadClientId"] = download_client_id
        if audiobook_id is not None:
            payload["audiobookId"] = audiobook_id
        return await self._post(f"{self.api_prefix}/download/send", data=payload)

    async def get_queue(self) -> list[dict[str, Any]]:
        """Live queue as reported by the download clients themselves."""
        snapshot = await self._get(f"{self.api_prefix}/download/queue")
        if isinstance(snapshot, dict):
            snapshot = snapshot.get("items") or snapshot.get("downloads") or []
        return cast("list[dict[str, Any]]", snapshot)

    async def get_download_records(self) -> list[dict[str, Any]]:
        """Listenarr's own download records, including failed and imported ones."""
        records = await self._get(f"{self.api_prefix}/downloads")
        if isinstance(records, dict):
            records = records.get("downloads") or records.get("items") or []
        return cast("list[dict[str, Any]]", records)

    async def import_from_prowlarr(self, url: str, api_key: str) -> dict[str, Any]:
        """Import indexers from a Prowlarr instance.

        Only Prowlarr indexers carrying category 3000/3030 are imported; the rest
        are skipped rather than rejected.
        """
        result = await self._post(
            f"{self.api_prefix}/indexers/prowlarr/import",
            data={"url": url, "apiKey": api_key},
        )
        return cast("dict[str, Any]", result)

    async def get_history(self, limit: int = 50) -> dict[str, Any]:
        """Grab/import history, newest first."""
        history = await self._get(f"{self.api_prefix}/history", params={"limit": limit})
        return cast("dict[str, Any]", history)

    async def get_indexers(self) -> list[dict[str, Any]]:
        """Configured indexers."""
        indexers = await self._get(f"{self.api_prefix}/indexers")
        return cast("list[dict[str, Any]]", indexers)

    async def get_root_folders(self) -> list[dict[str, Any]]:
        """Configured root folders."""
        folders = await self._get(f"{self.api_prefix}/rootfolders")
        return cast("list[dict[str, Any]]", folders)

    async def get_quality_profiles(self) -> list[dict[str, Any]]:
        """Configured quality profiles."""
        profiles = await self._get(f"{self.api_prefix}/qualityprofile")
        return cast("list[dict[str, Any]]", profiles)
