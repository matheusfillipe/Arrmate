"""Web routes: admin."""

from ._shared import (  # noqa: F401
    AuthRedirectException,
    Depends,
    Form,
    HTMLResponse,
    Request,
    _base_ctx,
    auth_router,
    get_current_user,
    require_admin,
    router,
    templates,
    user_db,
)


@router.get("/admin", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_page(request: Request):
    """Admin panel: user management, invites, pending requests."""
    users = user_db.list_users()
    invites = user_db.list_invites(include_used=False)
    pending = user_db.list_requests(status="pending")

    # Enrich requests with requester username
    user_map = {u["id"]: u["username"] for u in users}
    for req in pending:
        req["requester_name"] = user_map.get(req["requested_by"], "Unknown")

    return templates.TemplateResponse(
        request,
        "pages/admin.html",
        {
            **_base_ctx(request),
            "users": users,
            "invites": invites,
            "pending_requests": pending,
        },
    )


@router.post(
    "/admin/invite/create", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def admin_create_invite(
    request: Request,
    role: str = Form(default="user"),
    ttl_hours: int = Form(default=48),
):
    """Create an invite link."""
    current_user = get_current_user(request)
    admin_id = current_user["user_id"] if current_user else "system"
    token = user_db.create_invite(role, created_by=admin_id, ttl_hours=ttl_hours)
    invite_url = f"{request.base_url}web/register?token={token}"

    invites = user_db.list_invites(include_used=False)
    return templates.TemplateResponse(
        request,
        "partials/invite_list.html",
        {
            "invites": invites,
            "new_invite_url": invite_url,
            "new_invite_role": role,
        },
    )


@router.post(
    "/admin/invite/delete", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def admin_delete_invite(
    request: Request,
    token: str = Form(...),
):
    """Delete (revoke) an invite."""
    user_db.delete_invite(token)
    invites = user_db.list_invites(include_used=False)
    return templates.TemplateResponse(
        request,
        "partials/invite_list.html",
        {"invites": invites},
    )


@router.post(
    "/admin/user/{user_id}/role", response_class=HTMLResponse, dependencies=[Depends(require_admin)]
)
async def admin_set_role(
    request: Request,
    user_id: str,
    role: str = Form(...),
):
    """Change a user's role."""
    current_user = get_current_user(request)
    # Prevent admin from demoting themselves
    if current_user and current_user["user_id"] == user_id and role != "admin":
        pass  # Allow — admin can change their own role if they want
    user_db.update_user(user_id, role=role)
    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {"type": "success", "message": "Role updated"},
        headers={"HX-Trigger": "user-updated"},
    )


@router.post(
    "/admin/user/{user_id}/toggle",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def admin_toggle_user(
    request: Request,
    user_id: str,
    enabled: str = Form(...),
):
    """Enable or disable a user account."""
    new_state = enabled.lower() in ("true", "1", "on")
    user_db.update_user(user_id, enabled=new_state)
    label = "enabled" if new_state else "disabled"
    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {"type": "success", "message": f"User {label}"},
        headers={"HX-Trigger": "user-updated"},
    )


@router.post(
    "/admin/user/{user_id}/delete",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin)],
)
async def admin_delete_user(
    request: Request,
    user_id: str,
):
    """Delete a user account."""
    current_user = get_current_user(request)
    if current_user and current_user["user_id"] == user_id:
        return templates.TemplateResponse(
            request,
            "components/toast.html",
            {"type": "error", "message": "Cannot delete your own account"},
        )
    user_db.delete_user(user_id)
    return templates.TemplateResponse(
        request,
        "components/toast.html",
        {"type": "success", "message": "User deleted"},
        headers={"HX-Trigger": "user-updated"},
    )


@router.get("/api-tokens", response_class=HTMLResponse)
async def api_tokens_page(request: Request):
    """API token management page — shows the current user's tokens."""
    user = get_current_user(request)
    if not user:
        raise AuthRedirectException("/web/login?next=/web/api-tokens")
    tokens = user_db.list_api_tokens(user["user_id"])
    return templates.TemplateResponse(
        request,
        "pages/api_tokens.html",
        {"tokens": tokens, "new_token": None, **_base_ctx(request)},  # nosec B105
    )


@router.post("/api-tokens/create", response_class=HTMLResponse)
async def api_tokens_create(
    request: Request,
    name: str = Form(...),
    expires_days: str = Form(default=""),
):
    """Create a new API token for the current user."""
    user = get_current_user(request)
    if not user:
        raise AuthRedirectException("/web/login")

    if user.get("user_id") == "legacy":
        return templates.TemplateResponse(
            request,
            "pages/api_tokens.html",
            {
                "tokens": [],
                "error": "API tokens need a database account; log in with your Arrmate username.",
                **_base_ctx(request),
            },
            status_code=422,
        )

    exp_days = None
    if expires_days.strip():
        try:
            exp_days = int(expires_days.strip())
            if exp_days <= 0:
                exp_days = None
        except ValueError:
            exp_days = None

    _name = name.strip() or "API Token"
    _token_id, plain_token = user_db.create_api_token(
        user_id=user["user_id"],
        name=_name,
        expires_days=exp_days,
    )
    tokens = user_db.list_api_tokens(user["user_id"])
    return templates.TemplateResponse(
        request,
        "pages/api_tokens.html",
        {
            "tokens": tokens,
            "new_token": plain_token,
            "new_token_name": _name,
            **_base_ctx(request),
        },
    )


@router.delete("/api-tokens/{token_id}", response_class=HTMLResponse)
async def api_tokens_delete(request: Request, token_id: str):
    """Delete one of the current user's tokens."""
    user = get_current_user(request)
    if not user:
        raise AuthRedirectException("/web/login")
    user_db.delete_api_token(token_id, user["user_id"])
    tokens = user_db.list_api_tokens(user["user_id"])
    return templates.TemplateResponse(
        request,
        "partials/api_tokens_list.html",
        {"tokens": tokens, **_base_ctx(request)},
    )
