"""Shared base for the *arr family of API clients (Sonarr/Radarr/Lidarr/Readarr).

Every method here is endpoint-parameterized by the ``entity`` noun (series,
movie, artist, author) and ``api_prefix``; subclasses add only what is unique
to their service.
"""

from typing import Any, ClassVar, cast

import httpx

from .base import BaseMediaClient


class BaseArrClient(BaseMediaClient):
    """Client for the common *arr HTTP API shape."""

    entity: ClassVar[str] = "series"
    api_prefix: ClassVar[str] = "api/v3"
    search_command: ClassVar[str] = "SeriesSearch"

    async def test_connection(self) -> bool:
        """True when the system status endpoint answers with valid JSON."""
        try:
            await self.get_system_status()
            return True
        except (httpx.HTTPError, ValueError):
            return False

    async def get_system_status(self) -> dict[str, Any]:
        """Get system status and version."""
        status = await self._get(f"{self.api_prefix}/system/status")
        return cast("dict[str, Any]", status)

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search for items via the lookup endpoint."""
        results = await self._get(f"{self.api_prefix}/{self.entity}/lookup", params={"term": query})
        return cast("list[dict[str, Any]]", results)

    async def get_item(self, item_id: int) -> dict[str, Any]:
        """Get details of one item by ID."""
        item = await self._get(f"{self.api_prefix}/{self.entity}/{item_id}")
        return cast("dict[str, Any]", item)

    async def get_all_items(self) -> list[dict[str, Any]]:
        """Get every item in the library."""
        items = await self._get(f"{self.api_prefix}/{self.entity}")
        return cast("list[dict[str, Any]]", items)

    async def delete_item(self, item_id: int, delete_files: bool = False) -> bool:
        """Delete an item, optionally deleting its files."""
        await self._delete(
            f"{self.api_prefix}/{self.entity}/{item_id}?deleteFiles={str(delete_files).lower()}"
        )
        return True

    async def get_quality_profiles(self) -> list[dict[str, Any]]:
        """Get available quality profiles."""
        profiles = await self._get(f"{self.api_prefix}/qualityprofile")
        return cast("list[dict[str, Any]]", profiles)

    async def get_root_folders(self) -> list[dict[str, Any]]:
        """Get available root folders."""
        folders = await self._get(f"{self.api_prefix}/rootfolder")
        return cast("list[dict[str, Any]]", folders)

    async def trigger_item_search(self, item_id: int) -> dict[str, Any]:
        """Trigger an interactive search for every missing release of an item."""
        result = await self._post(
            f"{self.api_prefix}/command",
            data={"name": self.search_command, f"{self.entity}Id": item_id},
        )
        return cast("dict[str, Any]", result)

    async def get_tags(self) -> list[dict[str, Any]]:
        """Get all tags defined in the service."""
        tags = await self._get(f"{self.api_prefix}/tag")
        return cast("list[dict[str, Any]]", tags)

    async def create_tag(self, label: str) -> dict[str, Any]:
        """Create a new tag, returning the created tag dict."""
        tag = await self._post(f"{self.api_prefix}/tag", data={"label": label})
        return cast("dict[str, Any]", tag)

    async def delete_tag(self, tag_id: int) -> bool:
        """Delete a tag by ID."""
        await self._delete(f"{self.api_prefix}/tag/{tag_id}")
        return True

    async def add_tag_to_item(self, item_id: int, tag_id: int) -> dict[str, Any]:
        """Add a tag to an item (no-op when already present)."""
        item = await self.get_item(item_id)
        existing = item.get("tags", [])
        if tag_id not in existing:
            item["tags"] = [*existing, tag_id]
            updated = await self._put(f"{self.api_prefix}/{self.entity}/{item_id}", data=item)
            return cast("dict[str, Any]", updated)
        return item

    async def remove_tag_from_item(self, item_id: int, tag_id: int) -> dict[str, Any]:
        """Remove a tag from an item."""
        item = await self.get_item(item_id)
        item["tags"] = [tag for tag in item.get("tags", []) if tag != tag_id]
        updated = await self._put(f"{self.api_prefix}/{self.entity}/{item_id}", data=item)
        return cast("dict[str, Any]", updated)

    async def push_release(self, release: dict[str, Any]) -> dict[str, Any]:
        """Grab a specific release found by interactive search.

        ``release`` is the full dict as returned by interactive search; the
        service identifies it by its ``guid`` and ``indexerId``.
        """
        queued = await self._post(f"{self.api_prefix}/release", data=release)
        return cast("dict[str, Any]", queued)

    async def get_blocklist(self, page_size: int = 50) -> dict[str, Any]:
        """Get blocklisted releases, newest first."""
        blocklist = await self._get(
            f"{self.api_prefix}/blocklist",
            params={"pageSize": page_size, "sortKey": "date", "sortDirection": "descending"},
        )
        return cast("dict[str, Any]", blocklist)
