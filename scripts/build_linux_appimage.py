#!/usr/bin/env python3
"""Build and smoke-test the Linux Pandrator installer AppImage."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path


APPIMAGETOOL_URLS = {
    "x86_64": "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage",
    "aarch64": "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-aarch64.AppImage",
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
        urllib.request.urlretrieve(url, target)
    make_executable(target)
    return target


def resolve_appimagetool(explicit_path: str | None, repo_root: Path, machine: str) -> Path:
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


def build_pyinstaller_bundle(repo_root: Path) -> Path:
    spec_path = repo_root / "pandrator_installer_launcher_linux.spec"
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--clean",
            "--noconfirm",
            str(spec_path),
        ],
        cwd=repo_root,
    )

    bundle_dir = repo_root / "dist" / "PandratorInstaller"
    executable = bundle_dir / "PandratorInstaller"
    if not executable.exists():
        raise RuntimeError(f"PyInstaller output missing: {executable}")

    run([str(executable), "--self-check"], cwd=repo_root)
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    run([str(executable), "--gui-smoke-check"], cwd=repo_root, env=env)
    return bundle_dir


def stage_appdir(repo_root: Path, bundle_dir: Path, appdir: Path) -> None:
    if appdir.exists():
        shutil.rmtree(appdir)

    payload_dir = appdir / "usr" / "bin" / "PandratorInstaller"
    shutil.copytree(bundle_dir, payload_dir)

    icon_source = repo_root / "pandrator.png"
    icon_target = appdir / "usr" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "pandrator.png"
    icon_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icon_source, icon_target)
    shutil.copy2(icon_source, appdir / "pandrator.png")

    applications_dir = appdir / "usr" / "share" / "applications"
    applications_dir.mkdir(parents=True, exist_ok=True)
    desktop_file = applications_dir / "pandrator-installer.desktop"
    desktop_file.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                "Type=Application",
                "Name=Pandrator Installer",
                "Comment=Install and launch Pandrator",
                "Exec=PandratorInstaller",
                "Icon=pandrator",
                "Categories=AudioVideo;Audio;",
                "Terminal=false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    shutil.copy2(desktop_file, appdir / "pandrator-installer.desktop")

    apprun = appdir / "AppRun"
    apprun.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'HERE="$(dirname "$(readlink -f "$0")")"',
                'unset QT_PLUGIN_PATH',
                'unset QT_QPA_PLATFORM_PLUGIN_PATH',
                'exec "$HERE/usr/bin/PandratorInstaller/PandratorInstaller" "$@"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    make_executable(apprun)


def run_appimagetool(appimagetool: Path, appdir: Path, output_path: Path, repo_root: Path, machine: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    env = os.environ.copy()
    env["ARCH"] = machine
    env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    run([str(appimagetool), str(appdir), str(output_path)], cwd=repo_root, env=env)
    make_executable(output_path)


def smoke_test_appimage(appimage_path: Path, repo_root: Path) -> None:
    env = os.environ.copy()
    env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    run([str(appimage_path), "--self-check"], cwd=repo_root, env=env)
    run([str(appimage_path), "--gui-smoke-check"], cwd=repo_root, env=env)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build the Pandrator Linux installer AppImage.")
    parser.add_argument(
        "--appimagetool",
        default=None,
        help="Path to appimagetool. Defaults to PATH or a downloaded cached copy.",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Directory for the final AppImage.",
    )
    parser.add_argument(
        "--skip-pyinstaller",
        action="store_true",
        help="Reuse the existing dist/PandratorInstaller PyInstaller bundle.",
    )
    parser.add_argument(
        "--no-smoke-test",
        action="store_true",
        help="Do not run the final AppImage --self-check smoke test.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The AppImage installer must be built on Linux.")

    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    machine = normalized_machine()
    bundle_dir = repo_root / "dist" / "PandratorInstaller"

    if args.skip_pyinstaller:
        if not (bundle_dir / "PandratorInstaller").exists():
            raise RuntimeError(f"Existing PyInstaller bundle not found: {bundle_dir}")
    else:
        bundle_dir = build_pyinstaller_bundle(repo_root)

    appdir = repo_root / "build" / "PandratorInstaller.AppDir"
    stage_appdir(repo_root, bundle_dir, appdir)

    appimagetool = resolve_appimagetool(args.appimagetool, repo_root, machine)
    output_path = (repo_root / args.output_dir).resolve() / f"PandratorInstaller-{machine}.AppImage"
    run_appimagetool(appimagetool, appdir, output_path, repo_root, machine)

    if not args.no_smoke_test:
        smoke_test_appimage(output_path, repo_root)

    print(f"Built and verified installer AppImage: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
