"""Web routes: requests."""

import contextlib

from ._shared import (  # noqa: F401
    Form,
    HTMLResponse,
    HTTPException,
    RedirectResponse,
    Request,
    _base_ctx,
    auth_router,
    get_current_user,
    httpx,
    logger,
    notify_request_resolved,
    notify_request_submitted,
    router,
    settings,
    sqlite3,
    templates,
    user_db,
)


@router.get("/requests", response_class=HTMLResponse)
async def requests_page(request: Request):
    """Media requests page — all users can view and submit."""
    current_user = get_current_user(request)
    if not current_user:
        return RedirectResponse(url="/web/login", status_code=303)

    role = current_user.get("role", "user")
    if role in ("admin", "power_user"):
        all_requests = user_db.list_requests()
        # Enrich with usernames
        users = {u["id"]: u["username"] for u in user_db.list_users()}
        for req in all_requests:
            req["requester_name"] = users.get(req["requested_by"], "Unknown")
            if req.get("resolved_by"):
                req["resolver_name"] = users.get(req["resolved_by"], "Unknown")
            else:
                req["resolver_name"] = None
    else:
        all_requests = user_db.list_requests(user_id=current_user["user_id"])
        for req in all_requests:
            req["requester_name"] = current_user["username"]
            req["resolver_name"] = None

    return templates.TemplateResponse(
        request,
        "pages/requests.html",
        {
            **_base_ctx(request),
            "requests": all_requests,
            "current_user": current_user,
        },
    )


@router.post("/requests/new", response_class=HTMLResponse)
async def new_request(
    request: Request,
    title: str = Form(...),
    request_type: str = Form(default="media"),
    details: str = Form(default=""),
    media_type: str = Form(default=""),
):
    """Submit a new media request or issue report."""
    current_user = get_current_user(request)
    if not current_user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    req = user_db.create_request(
        request_type=request_type,
        user_id=current_user["user_id"],
        title=title,
        details=details,
        media_type=media_type,
    )
    try:
        await notify_request_submitted(req, settings)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
        logger.warning("Failed to send request notification: %s", e)

    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {
            "type": "success",
            "message": f"Request submitted: '{title}'",
        },
        headers={"HX-Trigger": "request-updated"},
    )


@router.post("/requests/{req_id}/resolve", response_class=HTMLResponse)
async def resolve_request(
    request: Request,
    req_id: str,
    status: str = Form(...),
    notes: str = Form(default=""),
):
    """Approve, complete, or reject a request — admin/power_user only."""
    current_user = get_current_user(request)
    if not current_user or current_user.get("role") not in ("admin", "power_user"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    req = user_db.get_request(req_id)
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")

    user_db.update_request(req_id, status=status, resolved_by=current_user["user_id"], notes=notes)
    updated_req = user_db.get_request(req_id)
    if updated_req:
        try:
            await notify_request_resolved(updated_req, settings)
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as e:
            logger.warning("Failed to send resolve notification: %s", e)

    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {
            "type": "success",
            "message": f"Request marked as {status}",
        },
        headers={"HX-Trigger": "request-updated"},
    )


@router.get("/notifications", response_class=HTMLResponse)
async def notifications_panel(request: Request):
    """HTMX partial: notification dropdown panel.

    When accessed directly (no HX-Request header — e.g. after a login
    redirect) we send the user to the main page instead of rendering a bare
    HTML fragment that would appear blank.
    """
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url="/web/", status_code=303)

    current_user = get_current_user(request)
    if not current_user or current_user.get("user_id") == "legacy":
        return templates.TemplateResponse(
            request,
            "partials/notifications_panel.html",
            {"notifications": [], "unread_count": 0},
        )
    notifications = user_db.get_notifications(current_user["user_id"])
    unread = sum(1 for n in notifications if not n["read"])
    return templates.TemplateResponse(
        request,
        "partials/notifications_panel.html",
        {
            "notifications": notifications,
            "unread_count": unread,
        },
    )


@router.get("/notifications/count", response_class=HTMLResponse)
async def notifications_count(request: Request):
    """HTMX partial: just the unread badge count (polled every 30s)."""
    current_user = get_current_user(request)
    unread = 0
    if current_user and current_user.get("user_id") not in (None, "legacy"):
        with contextlib.suppress(httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            unread = user_db.get_unread_count(current_user["user_id"])
    return templates.TemplateResponse(
        request,
        "partials/notification_count.html",
        {"unread_count": unread},
    )


@router.post("/notifications/read", response_class=HTMLResponse)
async def mark_notifications_read(request: Request):
    """Mark all notifications as read."""
    current_user = get_current_user(request)
    if current_user and current_user.get("user_id") not in (None, "legacy"):
        with contextlib.suppress(httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            user_db.mark_notifications_read(current_user["user_id"])
    return templates.TemplateResponse(
        request,
        "partials/notifications_panel.html",
        {"notifications": [], "unread_count": 0},
    )
