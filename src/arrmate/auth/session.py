"""Session token management via signed cookies."""

import os

from fastapi.responses import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from pydantic import BaseModel, ValidationError

from .models import LEGACY_USER_ID, SessionUser, UserRole

SESSION_COOKIE = "arrmate_session"
SESSION_MAX_AGE = 86400  # 24 hours

# Allow operators to explicitly override the secure flag via env var.
# Default: True (secure). Set COOKIE_SECURE=false for HTTP-only deployments.
_env = os.environ.get("COOKIE_SECURE", "true").lower()
_COOKIE_SECURE: bool = _env not in ("0", "false", "no")


class _SessionPayload(BaseModel):
    """What the cookie carries; `user` alone is the pre-multi-user format."""

    user_id: str | None = None
    username: str | None = None
    role: UserRole | None = None
    user: str | None = None


def create_session_token(user: SessionUser, secret_key: str) -> str:
    """Create a signed session token with full user info."""
    s = URLSafeTimedSerializer(secret_key)
    return s.dumps(user.model_dump(mode="json", include={"user_id", "username", "role"}))


def validate_session_token(token: str, secret_key: str) -> SessionUser | None:
    """Validate a session token. Old `{"user": name}` tokens are treated as the legacy admin."""
    s = URLSafeTimedSerializer(secret_key)
    try:
        payload = _SessionPayload.model_validate(s.loads(token, max_age=SESSION_MAX_AGE))
    except (BadSignature, SignatureExpired, ValidationError):
        return None
    if payload.user_id and payload.username and payload.role:
        return SessionUser(user_id=payload.user_id, username=payload.username, role=payload.role)
    if payload.user:
        return SessionUser(user_id=LEGACY_USER_ID, username=payload.user, role=UserRole.ADMIN)
    return None


def set_session_cookie(response: Response, token: str) -> None:
    """Set the session cookie on a response."""
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_COOKIE_SECURE,
    )


def clear_session_cookie(response: Response) -> None:
    """Delete the session cookie."""
    response.delete_cookie(SESSION_COOKIE, samesite="lax")
