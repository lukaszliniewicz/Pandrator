"""Local client credentials and browser recovery sessions."""

from .automation import (
    MANAGER_AUTOMATION_SCOPES,
    ManagerAutomationRateLimiter,
    ManagerAutomationService,
)
from .credentials import (
    RecoverySessionManager,
    derive_browser_session_keys,
    ensure_client_secret,
    protect_path,
    read_client_secret,
)

__all__ = [
    "RecoverySessionManager",
    "MANAGER_AUTOMATION_SCOPES",
    "ManagerAutomationRateLimiter",
    "ManagerAutomationService",
    "derive_browser_session_keys",
    "ensure_client_secret",
    "protect_path",
    "read_client_secret",
]
