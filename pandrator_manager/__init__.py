"""Pandrator's per-user installation and runtime control plane."""

from typing import TYPE_CHECKING

__version__ = "0.9.13"

if TYPE_CHECKING:
    from .application import ManagerApplication
    from .context import ManagerContext, WorkspaceLayout

__all__ = [
    "ManagerApplication",
    "ManagerContext",
    "WorkspaceLayout",
    "create_application",
]


def __getattr__(name: str):
    """Keep the convenience exports without eagerly loading the control plane."""

    if name in {"ManagerApplication", "create_application"}:
        from .application import ManagerApplication, create_application

        return {
            "ManagerApplication": ManagerApplication,
            "create_application": create_application,
        }[name]
    if name in {"ManagerContext", "WorkspaceLayout"}:
        from .context import ManagerContext, WorkspaceLayout

        return {
            "ManagerContext": ManagerContext,
            "WorkspaceLayout": WorkspaceLayout,
        }[name]
    raise AttributeError(name)
