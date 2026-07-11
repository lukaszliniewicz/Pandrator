"""Centralized path resolution for installed and source Pandrator runtimes."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


class PathBoundaryError(ValueError):
    """Raised when a requested managed path escapes an allowed root."""


def _platform_default_data_root() -> Path:
    system = platform.system().lower()
    if system == "windows":
        parent = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return parent / "Pandrator"
    if system == "darwin":
        return Path.home() / "Library" / "Application Support" / "Pandrator"
    parent = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return parent / "pandrator"


def resolve_data_root(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve the explicit, environment, or platform-default data root."""

    selected = value or os.environ.get("PANDRATOR_DATA_DIR")
    return Path(selected).expanduser().resolve() if selected else _platform_default_data_root().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class DataPaths:
    """All mutable Pandrator locations rooted outside the installed package."""

    root: Path

    @classmethod
    def from_value(cls, value: str | os.PathLike[str] | None = None) -> "DataPaths":
        return cls(resolve_data_root(value))

    @property
    def database(self) -> Path:
        return self.root / "pandrator.sqlite3"

    @property
    def legacy_database(self) -> Path:
        return self.root / "pandrator_state.sqlite3"

    @property
    def migration_marker(self) -> Path:
        return self.root / "migration-web-v1.json"

    @property
    def sessions(self) -> Path:
        return self.root / "sessions"

    @property
    def legacy_outputs(self) -> Path:
        return self.root / "Outputs"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def temporary(self) -> Path:
        return self.root / "tmp"

    @property
    def voices(self) -> Path:
        return self.root / "voices"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def secrets_file(self) -> Path:
        return self.root / "secrets.json"

    @property
    def instance_lock(self) -> Path:
        return self.root / "pandrator.instance.lock"

    def ensure(self) -> "DataPaths":
        for directory in (
            self.root,
            self.sessions,
            self.artifacts,
            self.uploads,
            self.temporary,
            self.voices,
            self.models,
            self.logs,
            self.backups,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def managed_path(self, relative_path: str | os.PathLike[str]) -> Path:
        """Resolve a managed relative path and reject root/symlink escape."""

        candidate = (self.root / Path(relative_path)).resolve()
        if not _is_within(candidate, self.root):
            raise PathBoundaryError(f"Managed path escapes the data root: {relative_path}")
        return candidate

    def relative_managed_path(self, path: str | os.PathLike[str]) -> str:
        candidate = Path(path).expanduser().resolve()
        if not _is_within(candidate, self.root):
            raise PathBoundaryError(f"Path is not managed by this data root: {path}")
        return candidate.relative_to(self.root).as_posix()

    def allowed_external_path(
        self,
        path: str | os.PathLike[str],
        allowed_roots: Iterable[str | os.PathLike[str]],
    ) -> Path:
        candidate = Path(path).expanduser().resolve(strict=True)
        roots = [Path(root).expanduser().resolve(strict=True) for root in allowed_roots]
        if not any(_is_within(candidate, root) for root in roots):
            raise PathBoundaryError(f"External path is outside configured source roots: {path}")
        return candidate

