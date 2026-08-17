"""Core data models for Arrmate."""

from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field


class MediaType(StrEnum):
    """Type of media being managed."""

    TV = "tv"
    MOVIE = "movie"
    MUSIC = "music"
    AUDIOBOOK = "audiobook"
    BOOK = "book"


class ActionType(StrEnum):
    """Type of action to perform on media."""

    REMOVE = "remove"
    SEARCH = "search"
    ADD = "add"
    UPGRADE = "upgrade"
    LIST = "list"
    INFO = "info"
    DELETE = "delete"
    DOWNLOAD_SUBTITLE = "download_subtitle"
    SYNC_SUBTITLES = "sync_subtitles"
    TRANSCODE = "transcode"
    RATE = "rate"
    BUTLER = "butler"
    QUEUE = "queue"
    HISTORY = "history"
    WANTED = "wanted"
    MONITOR = "monitor"
    UNMONITOR = "unmonitor"
    RENAME = "rename"
    RESCAN = "rescan"


USER_BLOCKED_ACTIONS = {ActionType.REMOVE, ActionType.DELETE, ActionType.TRANSCODE}
DESTRUCTIVE_ACTIONS = {ActionType.REMOVE, ActionType.DELETE}


class Intent(BaseModel):
    """Structured representation of user intent extracted from natural language."""

    action: ActionType = Field(description="The action to perform")
    media_type: MediaType = Field(description="Type of media (TV, movie, music, etc.)")
    title: str | None = Field(default=None, description="Title of the media item")
    season: int | None = Field(default=None, description="Season number (TV shows only)")
    episodes: list[int] | None = Field(default=None, description="Episode numbers (TV shows only)")
    criteria: dict[str, Any] | None = Field(
        default=None,
        description="Search/filter criteria (language, quality, etc.)",
    )
    keywords: list[str] = Field(
        default_factory=list,
        description="Thematic keywords for topic-based searches (e.g. ['christmas', 'holiday'])",
    )
    item_id: int | None = Field(
        default=None, description="Internal ID of the media item (populated during enrichment)"
    )
    series_id: int | None = Field(default=None, description="Internal series ID (TV shows only)")

    class Config:
        """Pydantic config."""

        use_enum_values = True


class ExecutionResult(BaseModel):
    """Result of executing an intent."""

    success: bool = Field(description="Whether the execution was successful")
    message: str = Field(description="Human-readable message about the result")
    data: dict[str, Any] | None = Field(
        default=None, description="Additional data returned from execution"
    )
    errors: list[str] | None = Field(default=None, description="List of errors if any")

    class Config:
        """Pydantic config."""

        json_schema_extra: ClassVar[dict[str, Any]] = {
            "example": {
                "success": True,
                "message": "Successfully removed 2 episodes from Angel Season 1",
                "data": {"removed_files": ["episode1.mkv", "episode2.mkv"]},
                "errors": None,
            }
        }


class ImplementationStatus(StrEnum):
    """Implementation status of a service."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    PLANNED = "planned"
    DEPRECATED = "deprecated"


class ServiceCapability(BaseModel):
    """Features supported by a service."""

    can_search: bool = Field(default=False, description="Can search for new media")
    can_add: bool = Field(default=False, description="Can add media to library")
    can_remove: bool = Field(default=False, description="Can remove media from library")
    can_upgrade: bool = Field(default=False, description="Can upgrade media quality")
    can_list: bool = Field(default=False, description="Can list library items")


class ServiceInfo(BaseModel):
    """Information about a discovered media service."""

    name: str = Field(description="Service name (sonarr, radarr, lidarr)")
    url: str = Field(description="Base URL of the service")
    api_key: str | None = Field(default=None, description="API key (masked)")
    available: bool = Field(description="Whether the service is reachable")
    version: str | None = Field(default=None, description="Service version")


class EnhancedServiceInfo(ServiceInfo):
    """Extended service info with implementation details."""

    implementation_status: ImplementationStatus = Field(
        description="Implementation status of this service"
    )
    api_version: str = Field(description="API version (v1, v3, custom)")
    capabilities: ServiceCapability = Field(description="Features supported by this service")
    media_type: str = Field(description="Type of media managed (TV, Movie, Music, etc.)")
    is_deprecated: bool = Field(default=False, description="Whether this service is deprecated")
    deprecation_message: str | None = Field(default=None, description="Deprecation warning message")
