"""Shared helpers for fast, isolated web tests."""

from __future__ import annotations

import shutil
import tempfile
import threading
from pathlib import Path

from pandrator.runtime import DataPaths
from pandrator.web.database import upgrade_database


_TEMPLATE_DIRECTORY = tempfile.TemporaryDirectory(prefix="pandrator-web-test-schema-")
_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_DATABASE: Path | None = None


def prepare_web_test_data_root(value: str | Path) -> DataPaths:
    """Create an isolated data root seeded from one migrated database template."""
    global _TEMPLATE_DATABASE

    paths = DataPaths.from_value(value).ensure()
    with _TEMPLATE_LOCK:
        if _TEMPLATE_DATABASE is None:
            template_paths = DataPaths.from_value(_TEMPLATE_DIRECTORY.name).ensure()
            upgrade_database(template_paths.database)
            _TEMPLATE_DATABASE = template_paths.database
        shutil.copy2(_TEMPLATE_DATABASE, paths.database)
    return paths
