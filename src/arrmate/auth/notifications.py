"""Webhook and in-app notification helpers."""

import logging

import httpx

from arrmate.config.settings import Settings

from . import user_db

logger = logging.getLogger(__name__)


async def send_slack(
    webhook_url: str,
    message: str,
    title: str = "",
    color: str = "#0ea5e9",
) -> bool:
    """Send a Slack webhook notification. Returns True on success."""
    if not webhook_url:
        return False
    payload = {
        "attachments": [
            {
                "color": color,
                "title": title,
                "text": message,
                "fallback": f"{title}: {message}" if title else message,
            }
        ]
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10)
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
    payload: dict = {
        "content": None,
        "embeds": [
            {
                "title": title,
                "description": message,
                "color": color,
            }
        ],
    }
    if not title:
        payload = {"content": message, "embeds": []}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(webhook_url, json=payload, timeout=10)
            return resp.status_code in (200, 204)
    except httpx.HTTPError as e:
        logger.warning("Discord webhook failed: %s", e)
        return False


async def notify_request_submitted(request: dict, settings_obj: Settings) -> None:
    """Notify admins/power_users about a new media request (in-app + webhooks)."""
    requester = user_db.get_user_by_id(request["requested_by"])
    requester_name = requester["username"] if requester else "Someone"
    title = f"New Request: {request['title']}"
    message = (
        f"{requester_name} submitted a {request['request_type']} request: '{request['title']}'"
    )

    for uid in user_db.get_admin_and_power_user_ids():
        if uid != request["requested_by"]:
            user_db.create_notification(uid, message, type="info", request_id=request["id"])

    slack_url = getattr(settings_obj, "slack_webhook_url", None)
    discord_url = getattr(settings_obj, "discord_webhook_url", None)
    if slack_url:
        await send_slack(slack_url, message, title=title, color="#0ea5e9")
    if discord_url:
        await send_discord(discord_url, message, title=title)


async def notify_request_resolved(request: dict, settings_obj: Settings) -> None:
    """Notify requester about their request being resolved (in-app + webhooks)."""
    resolver = (
        user_db.get_user_by_id(request["resolved_by"]) if request.get("resolved_by") else None
    )
    resolver_name = resolver["username"] if resolver else "Staff"
    status_label = {
        "completed": "fulfilled",
        "rejected": "rejected",
        "approved": "approved",
    }.get(request["status"], request["status"])

    title = f"Request {status_label.capitalize()}: {request['title']}"
    message = f"Your request '{request['title']}' has been {status_label} by {resolver_name}."
    if request.get("resolver_notes"):
        message += f" Note: {request['resolver_notes']}"

    notif_type = "success" if request["status"] == "completed" else "info"
    user_db.create_notification(
        request["requested_by"], message, type=notif_type, request_id=request["id"]
    )

    slack_url = getattr(settings_obj, "slack_webhook_url", None)
    discord_url = getattr(settings_obj, "discord_webhook_url", None)
    if slack_url:
        slack_color = "#22c55e" if request["status"] == "completed" else "#ef4444"
        await send_slack(slack_url, message, title=title, color=slack_color)
    if discord_url:
        discord_color = 0x22C55E if request["status"] == "completed" else 0xEF4444
        await send_discord(discord_url, message, title=title, color=discord_color)
