"""Runtime boundaries shared by browser, CLI, and worker processes."""

from .paths import DataPaths, PathBoundaryError, resolve_data_root

__all__ = ["DataPaths", "PathBoundaryError", "resolve_data_root"]

