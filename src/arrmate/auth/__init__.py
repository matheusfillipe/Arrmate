"""Authentication package for Arrmate."""

from . import user_db
from .manager import AuthManager

auth_manager = AuthManager()

__all__ = ["AuthManager", "auth_manager", "user_db"]
