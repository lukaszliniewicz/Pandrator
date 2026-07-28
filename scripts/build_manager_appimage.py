#!/usr/bin/env python3
"""Build and smoke-test the Qt-free Pandrator Manager AppImage."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from scripts.build_linux_appimage import (
        make_executable,
        normalized_machine,
        resolve_appimagetool,
        run_appimagetool,
        sha256_file,
    )
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from build_linux_appimage import (  # type: ignore[no-redef]
        make_executable,
        normalized_machine,
        resolve_appimagetool,
        run_appimagetool,
        sha256_file,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a wheel-fed, Qt-free Pandrator Manager AppImage and run its "
            "frozen self-check."
        )
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--wheel",
        type=Path,
        help="Manager wheel used to build the native bootstrap.",
    )
    source.add_argument(
        "--wheel-dir",
        type=Path,
        help="Directory containing exactly one pandrator-manager wheel.",
    )
    parser.add_argument(
        "--bootstrap",
        type=Path,
        help=(
            "Reuse an existing Linux PandratorManagerBootstrap executable. "
            "Cannot be combined with --wheel or --wheel-dir."
        ),
    )
    parser.add_argument(
        "--appimagetool",
        help="Path to appimagetool. Defaults to PATH or the pinned cache.",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Directory for the AppImage and SHA-256 checksum.",
    )
    parser.add_argument(
        "--no-smoke-test",
        action="store_true",
        help="Build without running the packaged frozen self-check.",
    )
    return parser.parse_args(argv)


def _within(root: Path, candidate: Path) -> bool:
    try:
        return candidate.resolve(strict=False).is_relative_to(
            root.resolve(strict=False)
        )
    except OSError:
        return False


def _reset_appdir(repo_root: Path, appdir: Path) -> None:
    build_root = (repo_root / "build").resolve(strict=False)
    resolved = appdir.resolve(strict=False)
    if resolved.parent != build_root or resolved.name != "PandratorManager.AppDir":
        raise RuntimeError(f"Refusing to replace an unexpected AppDir: {resolved}")
    if appdir.exists():
        if appdir.is_symlink() or not appdir.is_dir():
            raise RuntimeError(f"Unsafe manager AppDir: {appdir}")
        shutil.rmtree(appdir)
    appdir.mkdir(parents=True)


def _resolve_regular_file(
    path: Path,
    *,
    repo_root: Path,
    description: str,
) -> Path:
    selected = path if path.is_absolute() else repo_root / path
    resolved = selected.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise RuntimeError(f"{description} is not a regular file: {resolved}")
    return resolved


def build_bootstrap(
    repo_root: Path,
    *,
    wheel: Path | None,
    wheel_dir: Path | None,
) -> Path:
    command = [
        sys.executable,
        str(repo_root / "scripts" / "build_manager_bootstrap.py"),
    ]
    if wheel is not None:
        command.extend(("--wheel", str(wheel)))
    elif wheel_dir is not None:
        command.extend(("--wheel-dir", str(wheel_dir)))
    subprocess.run(command, cwd=repo_root, check=True)
    return _resolve_regular_file(
        repo_root / "dist" / "PandratorManagerBootstrap",
        repo_root=repo_root,
        description="Native manager bootstrap",
    )


def stage_appdir(
    repo_root: Path,
    bootstrap: Path,
    appdir: Path,
) -> None:
    _reset_appdir(repo_root, appdir)
    bootstrap = bootstrap.resolve(strict=True)
    if (
        not bootstrap.is_file()
        or bootstrap.is_symlink()
        or not os.access(bootstrap, os.X_OK)
    ):
        raise RuntimeError(
            f"Manager bootstrap is not a regular executable: {bootstrap}"
        )

    executable = appdir / "usr" / "bin" / "pandrator-manager-launcher"
    executable.parent.mkdir(parents=True)
    shutil.copy2(bootstrap, executable)
    make_executable(executable)

    icon_source = repo_root / "pandrator.png"
    if not icon_source.is_file() or icon_source.is_symlink():
        raise RuntimeError(f"Pandrator icon is unavailable: {icon_source}")
    icon = (
        appdir
        / "usr"
        / "share"
        / "icons"
        / "hicolor"
        / "256x256"
        / "apps"
        / "pandrator-manager.png"
    )
    icon.parent.mkdir(parents=True)
    shutil.copy2(icon_source, icon)
    shutil.copy2(icon_source, appdir / "pandrator-manager.png")
    shutil.copy2(icon_source, appdir / ".DirIcon")

    desktop_id = "io.github.lukaszliniewicz.PandratorManager.desktop"
    desktop = appdir / "usr" / "share" / "applications" / desktop_id
    desktop.parent.mkdir(parents=True)
    desktop.write_text(
        "\n".join(
            (
                "[Desktop Entry]",
                "Type=Application",
                "Name=Pandrator Manager",
                "Comment=Install, update, repair, and run Pandrator",
                "Exec=pandrator-manager-launcher",
                "Icon=pandrator-manager",
                "Categories=AudioVideo;Audio;",
                "Terminal=false",
                "",
            )
        ),
        encoding="utf-8",
    )
    shutil.copy2(desktop, appdir / desktop_id)

    metainfo = (
        appdir
        / "usr"
        / "share"
        / "metainfo"
        / "io.github.lukaszliniewicz.PandratorManager.appdata.xml"
    )
    metainfo.parent.mkdir(parents=True)
    metainfo.write_text(
        "\n".join(
            (
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<component type="desktop-application">',
                "  <id>io.github.lukaszliniewicz.PandratorManager</id>",
                "  <name>Pandrator Manager</name>",
                "  <summary>Install, update, repair, and run Pandrator</summary>",
                "  <metadata_license>CC0-1.0</metadata_license>",
                "  <project_license>MIT</project_license>",
                '  <developer id="io.github.lukaszliniewicz">',
                "    <name>Pandrator contributors</name>",
                "  </developer>",
                '  <content_rating type="oars-1.1"/>',
                "  <description>",
                "    <p>",
                "      Manage Pandrator and its local speech services through a",
                "      browser-based setup and recovery interface.",
                "    </p>",
                "  </description>",
                '  <launchable type="desktop-id">',
                f"    {desktop_id}",
                "  </launchable>",
                '  <url type="homepage">',
                "    https://github.com/lukaszliniewicz/Pandrator",
                "  </url>",
                "  <provides>",
                "    <binary>pandrator-manager-launcher</binary>",
                "  </provides>",
                "</component>",
                "",
            )
        ),
        encoding="utf-8",
    )

    apprun = appdir / "AppRun"
    apprun.write_text(
        "\n".join(
            (
                "#!/bin/sh",
                'HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"',
                'exec "$HERE/usr/bin/pandrator-manager-launcher" "$@"',
                "",
            )
        ),
        encoding="utf-8",
    )
    make_executable(apprun)


def smoke_test_appimage(appimage: Path, repo_root: Path) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
    result = subprocess.run(
        [str(appimage), "self-check"],
        cwd=repo_root,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Manager AppImage self-check returned invalid JSON.") from error
    if (
        not report.get("ok")
        or not report.get("frozen")
        or report.get("service") != "pandrator-manager-launcher"
    ):
        raise RuntimeError("Manager AppImage frozen self-check failed.")
    return report


def write_checksum(artifact: Path) -> tuple[Path, str]:
    digest = sha256_file(artifact)
    checksum = artifact.with_suffix(artifact.suffix + ".sha256")
    temporary = checksum.with_suffix(checksum.suffix + ".tmp")
    temporary.write_text(f"{digest}  {artifact.name}\n", encoding="ascii")
    os.replace(temporary, checksum)
    return checksum, digest


def main(argv: list[str] | None = None) -> int:
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The manager AppImage must be built on Linux.")
    args = parse_args(argv)
    if args.bootstrap is not None and (
        args.wheel is not None or args.wheel_dir is not None
    ):
        raise ValueError("--bootstrap cannot be combined with --wheel or --wheel-dir.")

    repo_root = Path(__file__).resolve().parents[1]
    if args.bootstrap is not None:
        bootstrap = _resolve_regular_file(
            args.bootstrap,
            repo_root=repo_root,
            description="Native manager bootstrap",
        )
    else:
        bootstrap = build_bootstrap(
            repo_root,
            wheel=args.wheel,
            wheel_dir=args.wheel_dir,
        )

    machine = normalized_machine()
    appdir = repo_root / "build" / "PandratorManager.AppDir"
    stage_appdir(repo_root, bootstrap, appdir)
    appimagetool = resolve_appimagetool(args.appimagetool, repo_root, machine)

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir
    output_dir = output_dir.resolve(strict=False)
    if not _within(repo_root, output_dir):
        raise RuntimeError(
            "Manager AppImage output must remain inside the repository."
        )
    sys.path.insert(0, str(repo_root))
    from pandrator_manager import __version__

    output = output_dir / f"PandratorManager-{__version__}-{machine}.AppImage"
    run_appimagetool(appimagetool, appdir, output, repo_root, machine)
    report = {} if args.no_smoke_test else smoke_test_appimage(output, repo_root)
    checksum, digest = write_checksum(output)
    canonical = output_dir / f"PandratorManager-{machine}.AppImage"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical_temporary = canonical.with_suffix(canonical.suffix + ".tmp")
    shutil.copy2(output, canonical_temporary)
    os.replace(canonical_temporary, canonical)
    canonical_checksum, canonical_digest = write_checksum(canonical)
    print(
        json.dumps(
            {
                "artifact": str(output),
                "checksum": str(checksum),
                "canonical_artifact": str(canonical),
                "canonical_checksum": str(canonical_checksum),
                "canonical_sha256": canonical_digest,
                "manager_version": report.get("manager_version"),
                "self_check": report,
                "sha256": digest,
                "size_bytes": output.stat().st_size,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
