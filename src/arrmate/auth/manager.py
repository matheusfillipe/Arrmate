"""Authentication manager — credential storage and verification."""

import logging
import os
import secrets
import tempfile
from pathlib import Path

import bcrypt as _bcrypt
from pydantic import ValidationError

from arrmate.config.settings import settings

from .models import LegacyAuthFile

logger = logging.getLogger(__name__)


class AuthManager:
    """Manages user credentials stored in a JSON file."""

    def __init__(self) -> None:
        self._data_dir = Path(settings.auth_data_dir)
        self._auth_file = self._data_dir / "auth.json"
        self._generated_secret: str | None = None

    def _read(self) -> LegacyAuthFile:
        """Read auth data from file."""
        if not self._auth_file.exists():
            return LegacyAuthFile()
        try:
            return LegacyAuthFile.model_validate_json(self._auth_file.read_text())
        except (ValidationError, OSError):
            logger.warning("Failed to read auth file, treating as empty")
            return LegacyAuthFile()

    def _write(self, data: LegacyAuthFile) -> None:
        """Atomically write auth data to file."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self._data_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(data.model_dump_json(indent=2))
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, str(self._auth_file))
        except OSError:
            # Clean up temp file on failure
            try:
                os.unlink(tmp_path)
            except OSError:
                logger.debug("temp credential file already gone", exc_info=True)
            raise

    def has_credentials(self) -> bool:
        """Check if any credentials are stored."""
        return bool(self._read().username)

    def is_auth_required(self) -> bool:
        """Check if authentication is currently required."""
        data = self._read()
        return bool(data.username and data.enabled)

    def set_credentials(self, username: str, password: str) -> None:
        """Create or replace credentials. Automatically enables auth."""
        password_hash = _bcrypt.hashpw(password.encode(), _bcrypt.gensalt()).decode()
        self._write(LegacyAuthFile(username=username, password_hash=password_hash, enabled=True))
        logger.info("Auth credentials set for user: %s", username)

    def verify(self, username: str, password: str) -> bool:
        """Verify username and password against stored credentials."""
        data = self._read()
        if not data.username or not data.password_hash or username != data.username:
            return False
        return _bcrypt.checkpw(password.encode(), data.password_hash.encode())

    def enable(self) -> None:
        """Enable authentication (credentials must exist)."""
        data = self._read()
        if not data.username:
            raise ValueError("No credentials to enable")
        data.enabled = True
        self._write(data)
        logger.info("Auth enabled")

    def disable(self) -> None:
        """Disable authentication without deleting credentials."""
        data = self._read()
        data.enabled = False
        self._write(data)
        logger.info("Auth disabled")

    def delete(self) -> None:
        """Delete all credentials."""
        if self._auth_file.exists():
            self._auth_file.unlink()
        logger.info("Auth credentials deleted")

    def get_username(self) -> str | None:
        """Get the stored username, if any."""
        return self._read().username

    def is_enabled(self) -> bool:
        """Check if auth is enabled (may be disabled even with credentials)."""
        return self._read().enabled

    def get_secret_key(self) -> str:
        """Get secret key for session signing. Auto-generates if not configured."""
        if settings.secret_key:
            return settings.secret_key
        if self._generated_secret is None:
            self._generated_secret = secrets.token_hex(32)
            logger.info("Auto-generated session secret key")
        return self._generated_secret
