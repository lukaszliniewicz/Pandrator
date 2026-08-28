#!/usr/bin/env python3
"""Build and smoke-test the standalone Pandrator installer executable."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def build_linux_appimage(repo_root: Path) -> int:
    from build_linux_appimage import main as build_appimage

    return build_appimage([])


def build_windows_executable(repo_root: Path) -> int:
    """Build the Manager bootstrap under the historical Windows filename.

    ``PandratorInstaller.exe`` used to embed the Qt installer and its own
    unauthenticated process supervisor.  Retain the familiar download name
    for one-time migrations, but make its payload the current Manager
    bootstrap so every normal launch enters the authenticated manager
    lifecycle.
    """

    from build_manager_bootstrap import main as build_manager_bootstrap

    if build_manager_bootstrap([]) != 0:
        raise RuntimeError("Pandrator Manager bootstrap build failed.")
    bootstrap = repo_root / "dist" / "PandratorManagerBootstrap.exe"
    executable = repo_root / "dist" / "PandratorInstaller.exe"
    if not bootstrap.is_file():
        raise RuntimeError(f"Manager bootstrap output missing: {bootstrap}")
    shutil.copy2(bootstrap, executable)
    subprocess.run([str(executable), "self-check"], check=True, cwd=repo_root)
    print(f"Built and verified Manager-based installer: {executable}")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]

    if sys.platform.startswith("linux"):
        return build_linux_appimage(repo_root)
    return build_windows_executable(repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
