"""Web routes: auth."""

from ._shared import (  # noqa: F401
    Form,
    HTMLResponse,
    HTTPException,
    Query,
    RedirectResponse,
    Request,
    _base_ctx,
    auth_manager,
    auth_router,
    build_plex_auth_url,
    clear_plex_state_cookie,
    clear_session_cookie,
    create_session_token,
    get_current_user,
    get_plex_friend_uuids,
    get_plex_state,
    get_plex_user,
    httpx,
    logger,
    login_limiter,
    plex_client_id,
    quote_plus,
    request_pin,
    router,
    safe_next_url,
    set_plex_state_cookie,
    set_session_cookie,
    settings,
    sqlite3,
    templates,
    user_db,
    validate_pin,
)


@auth_router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    next: str = Query(default="/web/"),
    error: str = Query(default=""),
):
    """Login page. Redirects to register only if truly no users exist."""
    try:
        no_users = not user_db.has_any_users()
    except sqlite3.Error:
        logger.warning("user db unavailable, serving login page", exc_info=True)
        no_users = False

    if no_users:
        return RedirectResponse(url="/web/register", status_code=303)

    # Show default-credentials hint when the admin account still has the factory password
    show_default_creds = False
    try:
        admin = user_db.get_user_by_username("admin")
        if admin and admin.get("must_change_password"):
            show_default_creds = True
    except sqlite3.Error:
        logger.warning("user db unavailable, hiding default-credentials hint", exc_info=True)

    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {
            "next": safe_next_url(next),
            "error": error,
            "show_default_creds": show_default_creds,
            "plex_sso_enabled": settings.plex_sso_enabled,
        },
    )


@auth_router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form(default="/web/"),
):
    """Process login form submission."""
    allowed, retry_after = await login_limiter.check(login_limiter._get_client_ip(request))
    if not allowed:
        from fastapi.responses import Response as _Response

        return _Response(
            content="Too many login attempts. Please try again later.",
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    # Try new multi-user DB first
    user = None
    try:
        user = user_db.verify_user(username, password)
    except (sqlite3.Error, ValueError):
        logger.warning("credential verification failed unexpectedly", exc_info=True)

    # Fall back to legacy single-user auth
    if user is None and auth_manager.verify(username, password):
        legacy_username = auth_manager.get_username() or username
        user = {"user_id": "legacy", "username": legacy_username, "role": "admin"}

    if user:
        uid = user.get("user_id") or user.get("id", "")
        token = create_session_token(
            uid,
            user["username"],
            user["role"],
            auth_manager.get_secret_key(),
        )
        # Redirect to change-password if required (e.g. default admin account)
        if user.get("must_change_password"):
            response = RedirectResponse(url="/web/change-password", status_code=303)
        else:
            redirect_url = safe_next_url(next)
            response = RedirectResponse(url=redirect_url, status_code=303)
        set_session_cookie(response, token)
        return response

    return templates.TemplateResponse(
        request,
        "pages/login.html",
        {
            "next": safe_next_url(next),
            "error": "Invalid username or password",
            "show_default_creds": False,
            "plex_sso_enabled": settings.plex_sso_enabled,
        },
        status_code=401,
    )


@auth_router.get("/logout")
async def logout():
    """Log out and redirect to login."""
    response = RedirectResponse(url="/web/login", status_code=303)
    clear_session_cookie(response)
    return response


@auth_router.get("/auth/plex/start")
async def plex_sso_start(
    request: Request,
    next: str = Query(default="/web/"),
):
    """Initiate Plex SSO login: request a PIN and redirect the user to plex.tv."""
    if not settings.plex_sso_enabled:
        return RedirectResponse(url="/web/login", status_code=303)

    # Apply the shared login rate limiter so Plex start counts against the same quota
    allowed, retry_after = await login_limiter.check(login_limiter._get_client_ip(request))
    if not allowed:
        return templates.TemplateResponse(
            request,
            "pages/login.html",
            {
                "next": safe_next_url(next),
                "error": "Too many login attempts. Please try again later.",
                "show_default_creds": False,
                "plex_sso_enabled": True,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    next_url = safe_next_url(next)
    secret_key = auth_manager.get_secret_key()
    client_id = plex_client_id(secret_key)

    try:
        pin_id, pin_code = await request_pin(client_id)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        logger.exception("Plex PIN request failed")
        return RedirectResponse(
            url="/web/login?error=" + quote_plus("Plex login unavailable. Please try again later."),
            status_code=303,
        )

    # Build the callback URL.  Use ARRMATE_BASE_URL when set (e.g. behind a reverse
    # proxy); otherwise respect X-Forwarded-Proto/Host headers (Traefik, nginx, etc.)
    # so the URL is always https:// even when TLS is terminated at the proxy layer.
    if settings.arrmate_base_url:
        base = settings.arrmate_base_url.rstrip("/")
    else:
        proto = request.headers.get("X-Forwarded-Proto") or request.url.scheme
        host = (
            request.headers.get("X-Forwarded-Host")
            or request.headers.get("Host")
            or request.url.netloc
        )
        base = f"{proto}://{host}"
    callback_url = f"{base}/web/auth/plex/callback"
    plex_url = build_plex_auth_url(client_id, pin_code, callback_url)
    logger.info(
        "Plex SSO start: arrmate_base_url=%r base=%r callback_url=%r plex_url=%r",
        settings.arrmate_base_url,
        base,
        callback_url,
        plex_url,
    )

    response = RedirectResponse(url=plex_url, status_code=302)
    set_plex_state_cookie(response, pin_id, next_url, secret_key)
    return response


@auth_router.get("/auth/plex/callback")
async def plex_sso_callback(request: Request):
    """Handle Plex OAuth callback: validate PIN, resolve identity, create session."""
    if not settings.plex_sso_enabled:
        return RedirectResponse(url="/web/login", status_code=303)

    def _login_error(msg: str):
        return RedirectResponse(url="/web/login?error=" + quote_plus(msg), status_code=303)

    secret_key = auth_manager.get_secret_key()
    client_id = plex_client_id(secret_key)

    # Read + validate the state cookie (signed, max 5 min old — CSRF protection)
    state = get_plex_state(request, secret_key)
    if not state:
        return _login_error("Plex login session expired. Please try again.")
    pin_id, next_url = state

    # Ask Plex whether the user has authorised the PIN
    try:
        auth_token = await validate_pin(pin_id, client_id)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        logger.exception("Plex PIN validation failed for pin_id=%s", pin_id)
        return _login_error("Plex login unavailable. Please try again later.")

    if not auth_token:
        return _login_error("Plex authorisation was not completed. Please try again.")

    # Fetch the Plex user's identity (UUID, username, email).
    # auth_token is intentionally NOT stored after this block.
    try:
        plex_user = await get_plex_user(auth_token)
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        logger.exception("Failed to fetch Plex user info")
        return _login_error("Could not retrieve Plex account information. Please try again.")
    finally:
        # Ensure auth_token reference is cleared even if get_plex_user raises
        del auth_token

    plex_uuid = plex_user.get("uuid") or plex_user.get("id")
    plex_username = plex_user.get("username") or plex_user.get("title") or "plex_user"
    plex_email = (plex_user.get("email") or "").strip().lower()

    if not plex_uuid:
        return _login_error("Could not verify Plex account identity. Please try again.")

    # Optional email allowlist
    if settings.plex_sso_allowed_emails:
        allowed_emails = {e.lower().strip() for e in settings.plex_sso_allowed_emails}
        if plex_email not in allowed_emails:
            logger.warning("Plex SSO: login denied for email=%r (not in allowlist)", plex_email)
            return _login_error("Your Plex account is not authorised to access this server.")

    # Look up or provision a local user record
    db_user = user_db.get_user_by_plex_id(str(plex_uuid))
    if not db_user:
        role = settings.plex_sso_default_role

        # Determine whether the new account should start enabled.
        # A user is auto-approved if:
        #   • require_approval is off, OR
        #   • verify_plex_friends is on AND they appear in the server's friends list.
        new_enabled = True
        if settings.plex_sso_require_approval:
            new_enabled = False
            if settings.plex_sso_verify_plex_friends and settings.plex_token:
                try:
                    friend_uuids = await get_plex_friend_uuids(settings.plex_token, client_id)
                    if str(plex_uuid) in friend_uuids:
                        new_enabled = True
                        logger.info("Plex SSO: auto-approved %s (Plex friend)", plex_username)
                except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
                    logger.exception("Plex SSO: could not fetch Plex friends list")

        db_user = user_db.create_plex_user(
            plex_id=str(plex_uuid),
            username=plex_username,
            email=plex_email or None,
            role=role,
            enabled=new_enabled,
        )
        if not db_user:
            # Username already taken by a local account — add a suffix and retry once
            db_user = user_db.create_plex_user(
                plex_id=str(plex_uuid),
                username=f"{plex_username}_plex",
                email=plex_email or None,
                role=role,
                enabled=new_enabled,
            )

        # Notify all admins about the new registration
        if db_user:
            admin_ids = user_db.get_admin_and_power_user_ids()
            status_label = "pending approval" if not new_enabled else "auto-approved"
            for admin_id in admin_ids:
                user_db.create_notification(
                    user_id=admin_id,
                    message=(
                        f"New Plex sign-in: '{plex_username}' registered via Plex SSO "
                        f"({status_label}). Enable their account in the Admin Panel."
                        if not new_enabled
                        else f"New Plex sign-in: '{plex_username}' signed in via Plex SSO."
                    ),
                    type="info",
                )

    if not db_user:
        logger.error("Plex SSO: could not create/find user for plex_uuid=%s", plex_uuid)
        return _login_error("Could not create your account. Please contact your administrator.")

    if not db_user.get("enabled"):
        return _login_error(
            "Your account is pending admin approval. "
            "Please contact your administrator to enable your access."
        )

    # Issue a normal arrmate session and send the user to their destination
    session_token = create_session_token(
        db_user["id"], db_user["username"], db_user["role"], secret_key
    )
    response = RedirectResponse(url=safe_next_url(next_url), status_code=303)
    set_session_cookie(response, session_token)
    clear_plex_state_cookie(response)
    return response


@auth_router.get("/change-password", response_class=HTMLResponse)
async def change_password_page(request: Request):
    """Show the change-password form."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/web/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "pages/change_password.html",
        {"error": None, **_base_ctx(request)},
    )


@auth_router.post("/change-password", response_class=HTMLResponse)
async def change_password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
):
    """Process the change-password form."""
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/web/login", status_code=303)

    def _error(msg: str):
        return templates.TemplateResponse(
            request,
            "pages/change_password.html",
            {"error": msg, **_base_ctx(request)},
            status_code=422,
        )

    if len(new_password) < 8:
        return _error("New password must be at least 8 characters.")
    if new_password != confirm_password:
        return _error("Passwords do not match.")

    uid = user.get("user_id") or user.get("id", "")
    # Verify current password against DB (legacy users can skip)
    if uid and uid != "legacy":
        db_user = user_db.get_user_by_id(uid)
        if db_user:
            verified = user_db.verify_user(db_user["username"], current_password)
            if not verified:
                return _error("Current password is incorrect.")
            user_db.change_password(uid, new_password)
        else:
            return _error("User not found.")
    else:
        return _error("Cannot change password for legacy accounts.")

    # If setup wizard has never been completed, send admin there now
    if user.get("role") == "admin" and not user_db.is_setup_complete():
        return RedirectResponse(url="/web/setup", status_code=303)

    return RedirectResponse(url="/web/", status_code=303)


@auth_router.get("/register", response_class=HTMLResponse)
async def register_page(
    request: Request,
    token: str = Query(default=""),
    error: str = Query(default=""),
):
    """Register page for invite tokens or first-admin setup."""
    try:
        no_users = not user_db.has_any_users()
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        no_users = False

    if no_users:
        # First-admin setup — no token required
        return templates.TemplateResponse(
            request,
            "pages/register.html",
            {
                "token": "",  # nosec B105
                "is_first_admin": True,
                "invite_role": "admin",
                "error": error,
            },
        )

    if not token:
        return RedirectResponse(url="/web/login", status_code=303)

    invite = user_db.validate_invite(token)
    if not invite:
        return templates.TemplateResponse(
            request,
            "pages/register.html",
            {
                "token": token,
                "is_first_admin": False,
                "invite_role": None,
                "error": "This invite link is invalid or has expired.",
            },
        )

    return templates.TemplateResponse(
        request,
        "pages/register.html",
        {
            "token": token,
            "is_first_admin": False,
            "invite_role": invite["role"],
            "error": error,
        },
    )


@auth_router.post("/register", response_class=HTMLResponse)
async def register_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    token: str = Form(default=""),
):
    """Process registration form."""
    # Validate input
    if len(username.strip()) < 1:
        error = "Username is required"
    elif len(password) < 8:
        error = "Password must be at least 8 characters"
    elif password != password_confirm:
        error = "Passwords do not match"
    else:
        error = ""

    if error:
        try:
            no_users = not user_db.has_any_users()
        except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
            no_users = False
        invite = user_db.validate_invite(token) if token else None
        return templates.TemplateResponse(
            request,
            "pages/register.html",
            {
                "token": token,
                "is_first_admin": no_users,
                "invite_role": invite["role"] if invite else ("admin" if no_users else None),
                "error": error,
            },
            status_code=422,
        )

    username = username.strip()

    try:
        no_users = not user_db.has_any_users()
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error):
        no_users = False

    try:
        if no_users:
            # Create first admin
            new_user = user_db.create_user(username, password, role="admin")
        elif token:
            new_user = user_db.use_invite(token, username, password)
        else:
            raise HTTPException(status_code=403, detail="Registration requires an invite")
    except HTTPException:
        raise
    except (httpx.HTTPError, KeyError, ValueError, sqlite3.Error) as exc:
        logger.exception("register_submit error: %s", exc)
        return templates.TemplateResponse(
            request,
            "pages/register.html",
            {
                "token": token,
                "is_first_admin": no_users,
                "invite_role": "admin" if no_users else None,
                "error": "An internal error occurred. Please try again.",
            },
            status_code=500,
        )

    if not new_user:
        invite = user_db.validate_invite(token) if token else None
        return templates.TemplateResponse(
            request,
            "pages/register.html",
            {
                "token": token,
                "is_first_admin": no_users,
                "invite_role": invite["role"] if invite else ("admin" if no_users else None),
                "error": "Username already taken or invite is invalid.",
            },
            status_code=422,
        )

    # Log the new user in
    session_token = create_session_token(
        new_user["id"],
        new_user["username"],
        new_user["role"],
        auth_manager.get_secret_key(),
    )
    response = RedirectResponse(url="/web/", status_code=303)
    set_session_cookie(response, session_token)
    return response
