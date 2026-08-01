"""Resolve Pandrator's version from one authoritative source."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path


def _source_tree_version() -> str | None:
    """Read the checkout version when this module is imported from source."""

    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        project = tomllib.loads(project_file.read_text(encoding="utf-8")).get(
            "project"
        )
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if not isinstance(project, dict) or project.get("name") != "pandrator":
        return None
    value = str(project.get("version") or "").strip()
    return value or None


def resolve_application_version() -> str:
    """Prefer checkout metadata, then the installed distribution metadata."""

    source_version = _source_tree_version()
    if source_version:
        return source_version
    try:
        return package_version("pandrator")
    except PackageNotFoundError:
        return "0+unknown"


PANDRATOR_VERSION = resolve_application_version()
