"""Records the auth package stores and hands to routes and templates."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LEGACY_USER_ID = "legacy"


class UserRole(StrEnum):
    ADMIN = "admin"
    POWER_USER = "power_user"
    USER = "user"


WRITE_ROLES = frozenset({UserRole.ADMIN, UserRole.POWER_USER})


class RequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    COMPLETED = "completed"
    REJECTED = "rejected"


class RequestType(StrEnum):
    MEDIA = "media"
    ISSUE = "issue"


AuthProvider = Literal["local", "plex"]
NotificationType = Literal["info", "success"]


class LegacyAuthFile(BaseModel):
    """The single-user credentials file that predates the user database."""

    username: str | None = None
    password_hash: str | None = None
    enabled: bool = False


class SessionUser(BaseModel):
    """Who a signed session cookie belongs to."""

    user_id: str
    username: str
    role: UserRole
    must_change_password: bool = False

    @property
    def is_legacy(self) -> bool:
        return self.user_id == LEGACY_USER_ID

    @property
    def can_write(self) -> bool:
        return self.role in WRITE_ROLES


class ApiUser(BaseModel):
    """Who a Bearer token belongs to."""

    user_id: str
    username: str
    role: UserRole
    token_id: str


class User(BaseModel):
    id: str
    username: str
    email: str | None = None
    password_hash: str
    role: UserRole
    enabled: bool
    created_at: str
    invited_by: str | None = None
    must_change_password: bool
    plex_id: str | None = None
    auth_provider: AuthProvider

    def session(self) -> SessionUser:
        return SessionUser(
            user_id=self.id,
            username=self.username,
            role=self.role,
            must_change_password=self.must_change_password,
        )


class Invite(BaseModel):
    token: str
    role: UserRole
    created_by: str
    created_at: str
    expires_at: str
    used: bool
    used_by: str | None = None
    used_at: str | None = None


class MediaRequest(BaseModel):
    id: str
    request_type: RequestType
    requested_by: str
    title: str
    details: str | None = None
    media_type: str | None = None
    status: RequestStatus
    created_at: str
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolver_notes: str | None = None
    notified_queued: bool
    notified_imported: bool


class Notification(BaseModel):
    id: str
    user_id: str
    message: str
    type: NotificationType
    read: bool
    request_id: str | None = None
    created_at: str


class ApiToken(BaseModel):
    id: str
    name: str
    token_prefix: str
    created_at: str
    last_used_at: str | None = None
    expires_at: str | None = None
    enabled: bool


class OwnedApiToken(ApiToken):
    username: str


class PlexPin(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: int
    code: str
    auth_token: str | None = Field(default=None, alias="authToken")


class PlexAccount(BaseModel):
    """The plex.tv profile behind an auth token, or one entry of its friends list."""

    model_config = ConfigDict(extra="ignore")

    id: int | None = None
    uuid: str | None = None
    username: str | None = None
    title: str | None = None
    email: str | None = None

    @property
    def identity(self) -> str | None:
        if self.uuid:
            return self.uuid
        return str(self.id) if self.id is not None else None


class PlexState(BaseModel):
    pin_id: int
    next: str
