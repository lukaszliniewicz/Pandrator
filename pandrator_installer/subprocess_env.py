"""Environment handling for processes launched outside the frozen installer."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


def external_subprocess_environment(
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment without PyInstaller's private Linux libraries.

    PyInstaller prepends its bundled library directory to ``LD_LIBRARY_PATH`` so
    the frozen installer can load its own dependencies.  Passing that value to
    an installed runtime or a host executable can instead make it load the
    bundle's older ``libstdc++``.  PyInstaller preserves the pre-bundle value in
    ``LD_LIBRARY_PATH_ORIG``; restore it when available and otherwise remove only
    entries inside ``sys._MEIPASS``.
    """

    environment = dict(os.environ if base_env is None else base_env)
    if not sys.platform.startswith("linux"):
        return environment

    original_library_path = environment.pop("LD_LIBRARY_PATH_ORIG", None)
    if original_library_path is not None:
        if original_library_path:
            environment["LD_LIBRARY_PATH"] = original_library_path
        else:
            environment.pop("LD_LIBRARY_PATH", None)
        return environment

    bundle_root = str(getattr(sys, "_MEIPASS", "") or "")
    current_library_path = environment.get("LD_LIBRARY_PATH", "")
    if not bundle_root or not current_library_path:
        return environment

    normalized_bundle_root = os.path.normcase(os.path.abspath(bundle_root))
    retained_paths = []
    for entry in current_library_path.split(os.pathsep):
        if not entry:
            continue
        normalized_entry = os.path.normcase(os.path.abspath(entry))
        if normalized_entry == normalized_bundle_root or normalized_entry.startswith(
            normalized_bundle_root + os.sep
        ):
            continue
        retained_paths.append(entry)

    if retained_paths:
        environment["LD_LIBRARY_PATH"] = os.pathsep.join(retained_paths)
    else:
        environment.pop("LD_LIBRARY_PATH", None)
    return environment
