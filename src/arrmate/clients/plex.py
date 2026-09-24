"""Plex Media Server API client.

Plex is a media server/player, not a downloader or content manager.
It serves existing files and does NOT fit into the executor routing path.
This client provides read-heavy operations (list, search, refresh) with
limited write operations (delete to trash, scan, mark watched/unwatched).
"""

from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .base_external import BaseExternalService, QueryParams

PlexLibraryType = Literal["movie", "show", "season", "episode", "artist", "album", "track"]


class _PlexRecord(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class PlexLibrary(_PlexRecord):
    key: str
    title: str
    type: str


class PlexAccount(_PlexRecord):
    id: int
    name: str | None = None

    @property
    def display_name(self) -> str:
        """Plex leaves the server owner's account (id 1) unnamed."""
        if self.name:
            return self.name
        return "Main User" if self.id == 1 else f"User {self.id}"


class PlexButlerTask(_PlexRecord):
    name: str
    title: str | None = None
    description: str | None = None
    enabled: bool | None = None
    running: bool | None = None


class PlexSessionUser(_PlexRecord):
    title: str | None = None
    thumb: str | None = None


class PlexPlayer(_PlexRecord):
    title: str | None = None
    platform: str | None = None
    state: str | None = None


class PlexSession(_PlexRecord):
    id: str | None = None
    bandwidth: int | None = None
    location: str | None = None


class PlexTranscodeSession(_PlexRecord):
    video_decision: str | None = None
    audio_decision: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None


class PlexMedia(_PlexRecord):
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None


class PlexMetadata(_PlexRecord):
    """One library item, history entry, session, or playlist; Plex shares the shape."""

    rating_key: str | None = None
    type: str | None = None
    title: str | None = None
    parent_title: str | None = None
    grandparent_title: str | None = None
    parent_index: int | None = None
    index: int | None = None
    year: int | None = None
    summary: str | None = None
    thumb: str | None = None
    grandparent_thumb: str | None = None
    composite: str | None = None
    duration: int | None = None
    view_offset: int | None = None
    added_at: int | None = None
    viewed_at: int | None = None
    account_id: int | None = Field(default=None, alias="accountID")
    leaf_count: int | None = None
    playlist_type: str | None = None
    session_key: str | None = None
    user: PlexSessionUser | None = Field(default=None, alias="User")
    player: PlexPlayer | None = Field(default=None, alias="Player")
    session: PlexSession | None = Field(default=None, alias="Session")
    transcode_session: PlexTranscodeSession | None = Field(default=None, alias="TranscodeSession")
    media: list[PlexMedia] = Field(default=[], alias="Media")

    @property
    def episode_code(self) -> str:
        return f"S{self.parent_index or 0:02d}E{self.index or 0:02d}"

    @property
    def progress_pct(self) -> int:
        return int((self.view_offset or 0) / self.duration * 100) if self.duration else 0

    @property
    def any_thumb(self) -> str | None:
        return self.thumb or self.grandparent_thumb


class PlexHub(_PlexRecord):
    type: str | None = None
    title: str | None = None
    metadata: list[PlexMetadata] = Field(default=[], alias="Metadata")


class _MediaContainer(_PlexRecord):
    machine_identifier: str | None = None
    version: str | None = None
    metadata: list[PlexMetadata] = Field(default=[], alias="Metadata")
    directory: list[PlexLibrary] = Field(default=[], alias="Directory")
    hub: list[PlexHub] = Field(default=[], alias="Hub")
    account: list[PlexAccount] = Field(default=[], alias="Account")
    butler_task: list[PlexButlerTask] = Field(default=[], alias="ButlerTask")


class _PlexResponse(_PlexRecord):
    media_container: _MediaContainer = Field(alias="MediaContainer")


class PlexClient(BaseExternalService):
    """Client for interacting with the Plex Media Server API.

    Uses X-Plex-Token authentication instead of X-Api-Key.
    Requests JSON responses via Accept header (Plex defaults to XML).
    """

    @property
    def client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client with Plex-specific headers."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={
                    "X-Plex-Token": self.api_key,
                    "X-Plex-Product": "Arrmate",
                    "X-Plex-Client-Identifier": "arrmate",
                    "Accept": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def _container(self, endpoint: str, params: QueryParams | None = None) -> _MediaContainer:
        return _PlexResponse.model_validate(
            await self._get(endpoint, params=params)
        ).media_container

    async def test_connection(self) -> bool:
        """Test connection to Plex by fetching server identity.

        Returns:
            True if connection is successful, False otherwise
        """
        try:
            return bool(await self.get_machine_identifier())
        except (httpx.HTTPError, ValueError):
            return False

    async def get_machine_identifier(self) -> str | None:
        """Get the server's unique machineIdentifier (needed for sharing via plex.tv).

        Returns:
            machineIdentifier string, or None on failure.
        """
        try:
            return (await self._container("/identity")).machine_identifier
        except (httpx.HTTPError, ValueError):
            return None

    async def get_version(self) -> str | None:
        """Get server version from identity endpoint.

        Returns:
            Version string or None
        """
        try:
            return (await self._container("/identity")).version
        except (httpx.HTTPError, ValueError):
            return None

    async def get_libraries(self) -> list[PlexLibrary]:
        """Get all library sections."""
        return (await self._container("/library/sections")).directory

    async def get_library_items(
        self, section_id: str, libtype: PlexLibraryType | None = None
    ) -> list[PlexMetadata]:
        """Get all items in a library section, optionally of one item type."""
        params: QueryParams = {"type": libtype} if libtype else {}
        return (await self._container(f"/library/sections/{section_id}/all", params)).metadata

    async def search(self, query: str, limit: int = 25) -> list[PlexHub]:
        """Search across all libraries; results come grouped into hubs by type."""
        params: QueryParams = {"query": query, "limit": limit}
        return (await self._container("/hubs/search/", params)).hub

    async def get_item(self, rating_key: str) -> PlexMetadata | None:
        """Get details for a specific media item."""
        items = (await self._container(f"/library/metadata/{rating_key}")).metadata
        return items[0] if items else None

    async def refresh_metadata(self, rating_key: str) -> bool:
        """Refresh metadata for a specific item.

        Args:
            rating_key: Plex ratingKey identifier

        Returns:
            True if request was accepted
        """
        try:
            url = f"{self.base_url}/library/metadata/{rating_key}/refresh"
            response = await self.client.put(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def scan_library(self, section_id: str) -> bool:
        """Trigger a scan/refresh of a library section.

        Args:
            section_id: Library section key

        Returns:
            True if request was accepted
        """
        try:
            url = f"{self.base_url}/library/sections/{section_id}/refresh"
            response = await self.client.get(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def scan_all_libraries(self) -> bool:
        """Trigger a scan of all library sections.

        Returns:
            True if request was accepted
        """
        try:
            url = f"{self.base_url}/library/sections/all/refresh"
            response = await self.client.get(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def delete_item(self, rating_key: str) -> bool:
        """Delete a media item (sends to trash).

        Args:
            rating_key: Plex ratingKey identifier

        Returns:
            True if deletion was accepted
        """
        try:
            url = f"{self.base_url}/library/metadata/{rating_key}"
            response = await self.client.delete(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def empty_trash(self, section_id: str) -> bool:
        """Permanently delete items in a section's trash.

        Args:
            section_id: Library section key

        Returns:
            True if request was accepted
        """
        try:
            url = f"{self.base_url}/library/sections/{section_id}/emptyTrash"
            response = await self.client.put(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def get_sessions(self) -> list[PlexMetadata]:
        """Get all active streaming sessions."""
        return (await self._container("/status/sessions")).metadata

    async def get_accounts(self) -> list[PlexAccount]:
        """Get the server's accounts; empty when Plex refuses the list."""
        try:
            return (await self._container("/accounts")).account
        except (httpx.HTTPError, ValueError):
            return []

    async def get_history(
        self,
        account_id: int | None = None,
        limit: int = 50,
        min_date: int | None = None,
    ) -> list[PlexMetadata]:
        """Get playback history, optionally filtered by user account and date.

        Args:
            account_id: Filter by Plex account ID (from get_accounts). None = all users.
            limit: Maximum number of history items to return.
            min_date: Unix timestamp — only return items viewed after this time.
        """
        params: QueryParams = {"X-Plex-Container-Size": limit, "sort": "viewedAt:desc"}
        if account_id:
            params["accountID"] = account_id
        if min_date:
            params["minDate"] = min_date
        return (await self._container("/status/sessions/history/all", params)).metadata

    async def get_continue_watching(self) -> list[PlexMetadata]:
        """Get items currently in progress (continue watching hub)."""
        try:
            items = (await self._container("/hubs/home/continueWatching")).metadata
            if items:
                return items
            on_deck = await self.get_on_deck()
            return [i for i in on_deck if (i.view_offset or 0) > 0]
        except (httpx.HTTPError, ValueError):
            return []

    async def get_on_deck(self) -> list[PlexMetadata]:
        """Get on-deck items (next episodes to watch for in-progress shows)."""
        return (await self._container("/library/onDeck")).metadata

    async def get_recently_added(self, limit: int = 25) -> list[PlexMetadata]:
        """Get recently added items across all libraries."""
        params: QueryParams = {"X-Plex-Container-Size": limit}
        return (await self._container("/library/recentlyAdded", params)).metadata

    async def terminate_session(
        self, session_id: str, reason: str = "Terminated by Arrmate"
    ) -> bool:
        """Terminate an active streaming session.

        Args:
            session_id: The Session.id UUID from get_sessions() (not sessionKey)
            reason: Message shown to the user on their player

        Returns:
            True if termination was accepted
        """
        try:
            url = f"{self.base_url}/status/sessions/terminate"
            response = await self.client.delete(
                url, params={"sessionId": session_id, "reason": reason}
            )
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def rate_item(self, rating_key: str, stars: float) -> bool:
        """Rate a media item.

        Args:
            rating_key: Plex ratingKey identifier
            stars: Rating from 1-5 (converted to Plex's 2-10 internal scale)

        Returns:
            True if rating was accepted
        """
        try:
            rate_params: dict[str, int | str] = {
                "key": rating_key,
                "identifier": "com.plexapp.plugins.library",
                "rating": int(max(1, min(5, stars)) * 2),
            }
            url = f"{self.base_url}/:/rate"
            response = await self.client.put(url, params=rate_params)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def get_butler_tasks(self) -> list[PlexButlerTask]:
        """Get available Butler maintenance tasks and their status."""
        try:
            return (await self._container("/butler")).butler_task
        except (httpx.HTTPError, ValueError):
            return []

    async def run_butler_task(self, task_name: str) -> bool:
        """Run a specific Butler maintenance task immediately.

        Args:
            task_name: Task name (e.g. CleanOldBundles, BackupDatabase)

        Returns:
            True if the task was started
        """
        try:
            url = f"{self.base_url}/butler/{task_name}"
            response = await self.client.post(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def detect_intro(self, rating_key: str) -> bool:
        """Trigger media analysis for an item (queues intro/chapter marker detection).

        Args:
            rating_key: Plex ratingKey — can be a series, season, or episode

        Returns:
            True if analysis was queued
        """
        try:
            url = f"{self.base_url}/library/metadata/{rating_key}/analyze"
            response = await self.client.put(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def detect_credits(self, rating_key: str) -> bool:
        """Trigger media analysis for an item (queues end-credit marker detection).

        Uses the same /analyze endpoint as intro detection — Plex queues all
        marker types (intro, credits, chapter thumbnails) together.

        Args:
            rating_key: Plex ratingKey — can be a series, season, or episode

        Returns:
            True if analysis was queued
        """
        try:
            url = f"{self.base_url}/library/metadata/{rating_key}/analyze"
            response = await self.client.put(url)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def get_playlists(self) -> list[PlexMetadata]:
        """Get all playlists on this server."""
        try:
            return (await self._container("/playlists/all")).metadata
        except (httpx.HTTPError, ValueError):
            return []

    async def get_playlist_items(self, playlist_id: str) -> list[PlexMetadata]:
        """Get items in a playlist, addressed by the playlist's ratingKey."""
        return (await self._container(f"/playlists/{playlist_id}/items")).metadata

    async def mark_watched(self, rating_key: str) -> bool:
        """Mark a media item as watched.

        Args:
            rating_key: Plex ratingKey identifier

        Returns:
            True if request was accepted
        """
        try:
            params = {
                "key": rating_key,
                "identifier": "com.plexapp.plugins.library",
            }
            url = f"{self.base_url}/:/scrobble"
            response = await self.client.get(url, params=params)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False

    async def mark_unwatched(self, rating_key: str) -> bool:
        """Mark a media item as unwatched.

        Args:
            rating_key: Plex ratingKey identifier

        Returns:
            True if request was accepted
        """
        try:
            params = {
                "key": rating_key,
                "identifier": "com.plexapp.plugins.library",
            }
            url = f"{self.base_url}/:/unscrobble"
            response = await self.client.get(url, params=params)
            return response.status_code in (200, 204)
        except (httpx.HTTPError, ValueError):
            return False
