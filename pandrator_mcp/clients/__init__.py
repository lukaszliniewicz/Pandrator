"""HTTP-only clients for target-bound Pandrator contracts."""

from .application import ApplicationClient
from .local_manager import (
    LocalManagerGateway,
    bootstrap_local_application,
    discover_local_application,
)
from .manager_gateway import (
    FallbackManagerGateway,
    ManagerGateway,
    ManagerUnavailableGateway,
)
from .manager_proxy import ApplicationProxyManagerGateway
from .manager_recovery import RecoveryManagerGateway

__all__ = [
    "ApplicationClient",
    "ApplicationProxyManagerGateway",
    "FallbackManagerGateway",
    "LocalManagerGateway",
    "ManagerGateway",
    "ManagerUnavailableGateway",
    "RecoveryManagerGateway",
    "bootstrap_local_application",
    "discover_local_application",
]
