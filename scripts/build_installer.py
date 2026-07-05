#!/usr/bin/env python3
"""Build and smoke-test the standalone Pandrator installer executable."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def build_linux_appimage(repo_root: Path) -> int:
    from build_linux_appimage import main as build_appimage

    return build_appimage([])


def build_windows_executable(repo_root: Path) -> int:
    spec_path = repo_root / "pandrator_installer_launcher.spec"
    executable = repo_root / "dist" / "PandratorInstaller.exe"

    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            str(spec_path),
        ],
        check=True,
        cwd=repo_root,
    )
    subprocess.run([str(executable), "--self-check"], check=True, cwd=repo_root)
    print(f"Built and verified installer: {executable}")
    return 0


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]

    if sys.platform.startswith("linux"):
        return build_linux_appimage(repo_root)
    return build_windows_executable(repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
