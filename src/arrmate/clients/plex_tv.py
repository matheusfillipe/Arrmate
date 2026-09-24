"""Plex.tv API client for user/sharing management.

This talks to plex.tv (not the local Plex Media Server).
Used for inviting friends, listing existing shares, and revoking access.
All calls require a valid X-Plex-Token from the server owner's account.
"""

import httpx
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel

PLEX_TV = "https://plex.tv"

_HEADERS = {
    "X-Plex-Product": "Arrmate",
    "X-Plex-Client-Identifier": "arrmate-server-share",
    "Accept": "application/json",
}


class _PlexTVRecord(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class PlexTVSection(_PlexTVRecord):
    title: str | None = None


class PlexTVServerShare(_PlexTVRecord):
    machine_identifier: str | None = None
    all_libraries: bool = False
    sections: list[PlexTVSection] = []


class PlexTVFriend(_PlexTVRecord):
    id: int
    title: str | None = None
    username: str | None = None
    email: str | None = None
    thumb: str | None = None
    servers: list[PlexTVServerShare] = []

    def share_on(self, machine_identifier: str | None) -> PlexTVServerShare | None:
        return next((s for s in self.servers if s.machine_identifier == machine_identifier), None)


class PlexHomeUser(_PlexTVRecord):
    id: int
    title: str | None = None
    username: str | None = None
    thumb: str | None = None
    admin: bool = False


class _HomeUserList(_PlexTVRecord):
    users: list[PlexHomeUser]


class _SwitchedUser(_PlexTVRecord):
    auth_token: str | None = Field(
        default=None, validation_alias=AliasChoices("authToken", "auth_token")
    )


class PlexTVClient:
    """Client for plex.tv user/sharing API."""

    def __init__(self, token: str, timeout: int = 20) -> None:
        self.token = token
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                headers={**_HEADERS, "X-Plex-Token": self.token},
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def get_friends(self) -> list[PlexTVFriend]:
        """Return all plex.tv friends (users who have been shared any server)."""
        resp = await self.client.get(f"{PLEX_TV}/api/v2/friends")
        resp.raise_for_status()
        return TypeAdapter(list[PlexTVFriend]).validate_python(resp.json())

    async def share_server(
        self,
        machine_identifier: str,
        invited_email: str,
        library_section_ids: list[int],
    ) -> None:
        """Invite a user by email and share selected library sections.

        Args:
            machine_identifier: Server machineIdentifier from /identity.
            invited_email: plex.tv email of the user to invite.
            library_section_ids: List of integer section keys to share.
                                 Empty list = share all libraries.

        Raises:
            httpx.HTTPStatusError: On API error (e.g. 400 already shared,
                                   422 invalid email, 401 bad token).
        """
        resp = await self.client.post(
            f"{PLEX_TV}/api/v2/shared_servers",
            json={
                "machineIdentifier": machine_identifier,
                "invitedEmail": invited_email,
                "librarySectionIds": library_section_ids,
            },
        )
        resp.raise_for_status()

    async def remove_friend(self, friend_id: int) -> bool:
        """Revoke a friend's access to all your shared servers.

        Args:
            friend_id: The numeric id from get_friends().

        Returns:
            True on success.
        """
        resp = await self.client.delete(f"{PLEX_TV}/api/v2/friends/{friend_id}")
        return resp.status_code in (200, 204)

    async def get_home_users(self) -> list[PlexHomeUser]:
        """Return all Plex home users (managed users on this account)."""
        resp = await self.client.get(f"{PLEX_TV}/api/v2/home/users")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            return _HomeUserList.model_validate(data).users
        return TypeAdapter(list[PlexHomeUser]).validate_python(data)

    async def switch_home_user(self, user_id: int) -> str | None:
        """Switch to a home user and return their auth token.

        For non-PIN-protected managed users only. Returns None on failure.

        Args:
            user_id: The numeric id from get_home_users().

        Returns:
            Auth token string for that user, or None on failure.
        """
        try:
            resp = await self.client.post(f"{PLEX_TV}/api/v2/home/users/{user_id}/switch")
            resp.raise_for_status()
            return _SwitchedUser.model_validate(resp.json()).auth_token
        except (httpx.HTTPError, ValueError):
            return None
