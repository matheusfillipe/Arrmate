"""Tests for authentication session management and user database."""

import logging
from unittest.mock import MagicMock

from itsdangerous import URLSafeTimedSerializer

from arrmate.auth.models import RequestStatus, RequestType, SessionUser, UserRole
from arrmate.auth.session import create_session_token, set_session_cookie, validate_session_token

# ── Session cookie flags ───────────────────────────────────────────────────────


def test_session_cookie_has_secure_flag():
    """Session cookie must set secure=True to prevent transmission over HTTP."""
    response = MagicMock()
    set_session_cookie(response, "test-token")
    call_kwargs = response.set_cookie.call_args.kwargs
    assert call_kwargs.get("secure") is True, "Cookie must have secure=True"


def test_session_cookie_has_httponly_flag():
    """Session cookie must be httponly to prevent JS access."""
    response = MagicMock()
    set_session_cookie(response, "test-token")
    assert response.set_cookie.call_args.kwargs.get("httponly") is True


def test_session_cookie_has_samesite_lax():
    """Session cookie must use samesite=lax."""
    response = MagicMock()
    set_session_cookie(response, "test-token")
    assert response.set_cookie.call_args.kwargs.get("samesite") == "lax"


# ── Default admin credentials ─────────────────────────────────────────────────


def test_default_admin_creation_does_not_log_password(tmp_path, caplog, monkeypatch):
    """Default admin creation must not log the plain-text default password."""
    from arrmate.auth import user_db as _user_db

    monkeypatch.setattr(_user_db, "_db_path", lambda: tmp_path / "users.db")

    with caplog.at_level(logging.DEBUG, logger="arrmate.auth.user_db"):
        _user_db._create_default_admin()

    full_log = " ".join(caplog.messages)
    assert "changeme123" not in full_log, "Plain-text default password must not appear in logs"


# ── Minimum password length ───────────────────────────────────────────────────


def test_minimum_password_length_is_at_least_eight():
    """Password minimum must be >= 8 characters (NIST SP 800-63B)."""
    import inspect

    from arrmate.interfaces.web.routes import auth as _routes

    source = inspect.getsource(_routes)
    assert "len(password) < 4" not in source, "Found 4-character minimum — must be at least 8"
    assert any(f"len(password) < {n}" in source for n in range(8, 20)), (
        "Expected minimum password length >= 8 not found in routes source"
    )


# ── Typed session and user records ────────────────────────────────────────────


def test_session_token_roundtrip():
    token = create_session_token(
        SessionUser(user_id="u1", username="ann", role=UserRole.POWER_USER), "k"
    )
    user = validate_session_token(token, "k")
    assert user == SessionUser(user_id="u1", username="ann", role=UserRole.POWER_USER)
    assert user.can_write


def test_pre_multi_user_session_token_is_the_legacy_admin():
    token = URLSafeTimedSerializer("k").dumps({"user": "old"})
    user = validate_session_token(token, "k")
    assert user is not None
    assert user.is_legacy
    assert user.role == UserRole.ADMIN


def test_session_token_with_unknown_role_is_rejected():
    token = URLSafeTimedSerializer("k").dumps({"user_id": "u", "username": "u", "role": "root"})
    assert validate_session_token(token, "k") is None


def test_user_rows_come_back_typed(tmp_user_db):
    user = tmp_user_db.create_user("ann", "longenough", role=UserRole.POWER_USER)
    assert user is not None
    assert user.enabled is True
    assert user.must_change_password is False
    assert tmp_user_db.verify_user("ann", "longenough") == user

    assert tmp_user_db.update_user(user.id, enabled=False)
    assert tmp_user_db.verify_user("ann", "longenough") is None


def test_invite_creates_user_with_its_role(tmp_user_db):
    token = tmp_user_db.create_invite(UserRole.POWER_USER, created_by="admin")
    user = tmp_user_db.use_invite(token, "bob", "longenough")
    assert user is not None
    assert user.role == UserRole.POWER_USER
    assert tmp_user_db.validate_invite(token) is None


def test_request_notification_and_token_rows(tmp_user_db):
    user = tmp_user_db.create_user("cat", "longenough")
    req = tmp_user_db.create_request(RequestType.ISSUE, user.id, "Dune")
    assert req.status == RequestStatus.PENDING
    assert req.notified_queued is False

    tmp_user_db.update_request(req.id, RequestStatus.COMPLETED, resolved_by="admin")
    assert tmp_user_db.list_requests(status=RequestStatus.COMPLETED)[0].resolved_by == "admin"

    tmp_user_db.create_notification(user.id, "hi", type="success", request_id=req.id)
    [notification] = tmp_user_db.get_notifications(user.id)
    assert notification.read is False
    assert notification.type == "success"

    _token_id, plain = tmp_user_db.create_api_token(user.id, "cli")
    api_user = tmp_user_db.validate_api_token(plain)
    assert api_user is not None
    assert api_user.username == "cat"
    assert tmp_user_db.list_api_tokens(user.id)[0].name == "cli"
