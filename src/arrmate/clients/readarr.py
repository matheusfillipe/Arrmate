"""Readarr API client implementation."""

import logging
from typing import Any

from .base_arr import BaseArrClient

logger = logging.getLogger(__name__)


class ReadarrClient(BaseArrClient):
    """Client for Readarr v1 API (Books/Audiobooks).

    WARNING: Readarr project is deprecated. This client is provided
    for compatibility with existing instances only.
    """

    entity = "author"
    api_prefix = "api/v1"
    search_command = "AuthorSearch"

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search for books/audiobooks by title or author.

        The v1 API exposes a flat /search endpoint with no author/lookup route.
        """
        return await self._get("api/v1/search", params={"term": query})

    async def add_author(
        self,
        foreign_author_id: str,
        author_name: str,
        quality_profile_id: int,
        metadata_profile_id: int,
        root_folder_path: str,
        monitored: bool = True,
        search_for_missing: bool = True,
    ) -> dict[str, Any]:
        """Add a new author to the library."""
        data = {
            "foreignAuthorId": foreign_author_id,
            "authorName": author_name,
            "qualityProfileId": quality_profile_id,
            "metadataProfileId": metadata_profile_id,
            "rootFolderPath": root_folder_path,
            "monitored": monitored,
            "addOptions": {"searchForMissingBooks": search_for_missing},
        }
        return await self._post("api/v1/author", data=data)

    async def get_books(self, author_id: int) -> list[dict[str, Any]]:
        """Get books for an author."""
        return await self._get("api/v1/book", params={"authorId": author_id})

    async def get_book_files(self, author_id: int) -> list[dict[str, Any]]:
        """Get book files for an author."""
        return await self._get("api/v1/bookfile", params={"authorId": author_id})

    async def delete_book_file(self, file_id: int) -> bool:
        """Delete a book file."""
        await self._delete(f"api/v1/bookfile/{file_id}")
        return True

    async def trigger_book_search(self, book_ids: list[int]) -> dict[str, Any]:
        """Trigger a search for specific books."""
        return await self._post(
            "api/v1/command",
            data={"name": "BookSearch", "bookIds": book_ids},
        )

    async def get_metadata_profiles(self) -> list[dict[str, Any]]:
        """Get available metadata profiles."""
        return await self._get("api/v1/metadataprofile")
