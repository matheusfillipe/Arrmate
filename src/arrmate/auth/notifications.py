"""Webhook and in-app notification helpers."""

import logging

import httpx
from pydantic import BaseModel

from arrmate.config.settings import Settings

from . import user_db
from .models import MediaRequest, RequestStatus

logger = logging.getLogger(__name__)

_STATUS_LABELS = {
    RequestStatus.COMPLETED: "fulfilled",
    RequestStatus.REJECTED: "rejected",
    RequestStatus.APPROVED: "approved",
    RequestStatus.PENDING: "pending",
}


class _SlackAttachment(BaseModel):
    color: str
    title: str
    text: str
    fallback: str


class _SlackPayload(BaseModel):
    attachments: list[_SlackAttachment]


class _DiscordEmbed(BaseModel):
    title: str
    description: str
    color: int


class _DiscordPayload(BaseModel):
    content: str | None
    embeds: list[_DiscordEmbed]


async def send_slack(
    webhook_url: str,
    message: str,
    title: str = "",
    color: str = "#0ea5e9",
) -> bool:
    """Send a Slack webhook notification. Returns True on success."""
    if not webhook_url:
        return False
    payload = _SlackPayload(
        attachments=[
            _SlackAttachment(
                color=color,
                title=title,
                text=message,
                fallback=f"{title}: {message}" if title else message,
            )
        ]
    )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload.model_dump(), timeout=10)
            return resp.status_code == 200
    except httpx.HTTPError as e:
        logger.warning("Slack webhook failed: %s", e)
        return False


async def send_discord(
    webhook_url: str,
    message: str,
    title: str = "",
    color: int = 0x0EA5E9,
) -> bool:
    """Send a Discord webhook notification. Returns True on success."""
    if not webhook_url:
        return False
    if title:
        payload = _DiscordPayload(
            content=None,
            embeds=[_DiscordEmbed(title=title, description=message, color=color)],
        )
    else:
        payload = _DiscordPayload(content=message, embeds=[])
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload.model_dump(), timeout=10)
            return resp.status_code in (200, 204)
    except httpx.HTTPError as e:
        logger.warning("Discord webhook failed: %s", e)
        return False


async def notify_request_submitted(request: MediaRequest, settings_obj: Settings) -> None:
    """Notify admins/power_users about a new media request (in-app + webhooks)."""
    requester = user_db.get_user_by_id(request.requested_by)
    requester_name = requester.username if requester else "Someone"
    title = f"New Request: {request.title}"
    message = f"{requester_name} submitted a {request.request_type} request: '{request.title}'"

    for uid in user_db.get_admin_and_power_user_ids():
        if uid != request.requested_by:
            user_db.create_notification(uid, message, type="info", request_id=request.id)

    if settings_obj.slack_webhook_url:
        await send_slack(settings_obj.slack_webhook_url, message, title=title, color="#0ea5e9")
    if settings_obj.discord_webhook_url:
        await send_discord(settings_obj.discord_webhook_url, message, title=title)


async def notify_request_resolved(request: MediaRequest, settings_obj: Settings) -> None:
    """Notify requester about their request being resolved (in-app + webhooks)."""
    resolver = user_db.get_user_by_id(request.resolved_by) if request.resolved_by else None
    resolver_name = resolver.username if resolver else "Staff"
    status_label = _STATUS_LABELS[request.status]
    completed = request.status == RequestStatus.COMPLETED

    title = f"Request {status_label.capitalize()}: {request.title}"
    message = f"Your request '{request.title}' has been {status_label} by {resolver_name}."
    if request.resolver_notes:
        message += f" Note: {request.resolver_notes}"

    user_db.create_notification(
        request.requested_by,
        message,
        type="success" if completed else "info",
        request_id=request.id,
    )

    if settings_obj.slack_webhook_url:
        slack_color = "#22c55e" if completed else "#ef4444"
        await send_slack(settings_obj.slack_webhook_url, message, title=title, color=slack_color)
    if settings_obj.discord_webhook_url:
        discord_color = 0x22C55E if completed else 0xEF4444
        await send_discord(
            settings_obj.discord_webhook_url, message, title=title, color=discord_color
        )
