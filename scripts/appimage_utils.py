"""Shared helpers for producing AppImages with a pinned appimagetool."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import stat
import subprocess
import urllib.request
from pathlib import Path

APPIMAGETOOL_URLS = {
    "x86_64": "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage",
    "aarch64": "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage",
}

APPIMAGETOOL_SHA256 = {
    "x86_64": "a6d71e2b6cd66f8e8d16c37ad164658985e0cf5fcaa950c90a482890cb9d13e0",
    "aarch64": "1b00524ba8c6b678dc15ef88a5c25ec24def36cdfc7e3abb32ddcd068e8007fe",
}


def normalized_machine() -> str:
    machine = (platform.machine() or "").lower()
    if machine in {"amd64", "x64"}:
        return "x86_64"
    if machine in {"arm64"}:
        return "aarch64"
    return machine


def make_executable(path: Path) -> None:
    current_mode = path.stat().st_mode
    path.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_appimagetool(path: Path, machine: str) -> None:
    expected = APPIMAGETOOL_SHA256[machine]
    actual = sha256_file(path)
    if actual != expected:
        raise RuntimeError(
            f"appimagetool checksum mismatch for {path}: expected {expected}, got {actual}"
        )


def run(command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, check=True, cwd=str(cwd), env=env)


def download_appimagetool(cache_dir: Path, machine: str) -> Path:
    url = APPIMAGETOOL_URLS.get(machine)
    if not url:
        raise RuntimeError(f"Unsupported AppImage build architecture: {machine}")

    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"appimagetool-{machine}.AppImage"
    if not target.exists():
        print(f"Downloading appimagetool: {url}")
        temporary_target = target.with_suffix(f"{target.suffix}.download")
        try:
            urllib.request.urlretrieve(url, temporary_target)
            verify_appimagetool(temporary_target, machine)
            temporary_target.replace(target)
        finally:
            temporary_target.unlink(missing_ok=True)
    else:
        verify_appimagetool(target, machine)
    make_executable(target)
    return target


def resolve_appimagetool(
    explicit_path: str | None, repo_root: Path, machine: str
) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"appimagetool not found: {path}")
        make_executable(path)
        return path

    discovered = shutil.which("appimagetool")
    if discovered:
        return Path(discovered).resolve()

    return download_appimagetool(repo_root / ".appimage-tools", machine)


def run_appimagetool(
    appimagetool: Path,
    appdir: Path,
    output_path: Path,
    repo_root: Path,
    machine: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    env = os.environ.copy()
    env["ARCH"] = machine
    env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    run([str(appimagetool), str(appdir), str(output_path)], cwd=repo_root, env=env)
    make_executable(output_path)
