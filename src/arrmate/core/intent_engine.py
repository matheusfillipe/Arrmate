"""Intent validation and enrichment engine."""

from collections.abc import Sequence

from arrmate.clients.base_arr import ArrItem
from arrmate.clients.discovery import ArrClient, get_client_for_media_type
from arrmate.clients.lidarr import LidarrClient
from arrmate.clients.radarr import RadarrClient
from arrmate.clients.readarr import ReadarrClient
from arrmate.clients.sonarr import SonarrClient

from .models import Intent


def _library_match(items: Sequence[ArrItem], title: str) -> int | None:
    """The id of the item named ``title``, else of the first whose title contains it."""
    for item in items:
        if item.title.lower() == title:
            return item.id
    for item in items:
        if title in item.title.lower():
            return item.id
    return None


class IntentEngine:
    """Validates and enriches Intent objects with additional context."""

    async def enrich(self, intent: Intent) -> Intent:
        """Enrich an intent with additional context.

        This matches the title against the library, then against the service lookup, to
        fill in the item IDs.

        Args:
            intent: The intent to enrich

        Returns:
            Enriched intent with additional fields populated

        Raises:
            ValueError: If required information cannot be resolved
        """
        # Transcode actions handle their own file discovery — skip enrichment
        if intent.action == "transcode":
            return intent

        # Get the appropriate client for this media type
        client = get_client_for_media_type(intent.media_type)

        try:
            if intent.title and not intent.item_id:
                await self._resolve_title(intent, client)
            return intent
        finally:
            await client.close()

    async def _resolve_title(self, intent: Intent, client: ArrClient) -> None:
        """Point the intent at the library item with its title, else at the first lookup match.

        A Sonarr lookup match sets ``item_id`` to the TVDB id the executor adds it by. Other
        lookup matches leave ``item_id`` unset, because the executor reads it as a library id
        for movies, artists and authors. Raises ValueError when neither the library nor the
        lookup knows the title.
        """
        title = intent.title or ""
        match client:
            case SonarrClient() | RadarrClient() | ReadarrClient():
                library_id = _library_match(await client.get_all_items(), title.lower())
            case LidarrClient():
                # Lidarr artists carry an artistName and no title, so we only look them up.
                library_id = None
        if library_id is not None:
            intent.item_id = library_id
            if isinstance(client, SonarrClient):
                intent.series_id = library_id
            return

        if isinstance(client, SonarrClient):
            series = await client.search(title)
            if series:
                intent.item_id = series[0].tvdb_id
            found = bool(series)
        else:
            found = bool(await client.search(title))
        if not found:
            raise ValueError(f"Could not find '{intent.title}' in library or search results")

    def validate(self, intent: Intent) -> list[str]:
        """Validate an intent and return any validation errors.

        Args:
            intent: Intent to validate

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        # Check required fields based on action
        if intent.action in ["remove", "delete", "info"] and not intent.title:
            errors.append("Title is required for this action")

        if intent.action == "add" and not intent.title:
            errors.append("Title is required to add media")

        # TV-specific validation
        if (
            intent.media_type == "tv"
            and intent.action in ["remove", "delete"]
            and intent.episodes
            and not intent.season
        ):
            errors.append("Season number is required when specifying episodes")

        return errors
