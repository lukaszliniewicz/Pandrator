#!/usr/bin/env python3
"""Package a frozen manager bootstrap as a signed-release-ready runtime ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import stat
import sys
import zipfile
from pathlib import Path

from packaging.version import Version


def _release_platform(
    *,
    system: str | None = None,
    machine: str | None = None,
) -> str:
    selected_system = str(system or sys.platform).casefold()
    selected_machine = str(machine or platform.machine()).casefold()
    if selected_system in {"win32", "windows", "nt"}:
        operating_system = "windows"
    elif selected_system.startswith("linux"):
        operating_system = "linux"
    else:
        raise RuntimeError(
            "Manager runtime release bundles are supported only on Windows "
            "and Linux."
        )
    if selected_machine in {"amd64", "x86_64", "x64"}:
        architecture = "x86_64"
    elif selected_machine in {"arm64", "aarch64"}:
        architecture = "aarch64"
    else:
        raise RuntimeError(
            f"Unsupported Manager runtime architecture: {selected_machine}"
        )
    return f"{operating_system}-{architecture}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_bytes(
    archive: zipfile.ZipFile,
    name: str,
    payload: bytes,
    *,
    executable: bool = False,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    mode = (
        stat.S_IFREG
        | stat.S_IRUSR
        | stat.S_IWUSR
        | (stat.S_IXUSR if executable else 0)
    )
    info.external_attr = mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, payload)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a deterministic Pandrator Manager runtime bundle."
    )
    suffix = ".exe" if sys.platform == "win32" else ""
    parser.add_argument(
        "--bootstrap",
        type=Path,
        default=Path("dist") / f"PandratorManagerBootstrap{suffix}",
    )
    parser.add_argument("--version", default=None)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    bootstrap = (
        args.bootstrap
        if args.bootstrap.is_absolute()
        else repo_root / args.bootstrap
    ).resolve(strict=True)
    if not bootstrap.is_file() or bootstrap.is_symlink():
        raise RuntimeError(f"Bootstrap is not a regular file: {bootstrap}")

    sys.path.insert(0, str(repo_root))
    from pandrator_manager import __version__

    version = str(args.version or __version__)
    Version(version)
    output = args.output or (
        repo_root
        / "dist"
        / f"pandrator-manager-{version}-{_release_platform()}.zip"
    )
    if not output.is_absolute():
        output = repo_root / output
    output = output.resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    runtime_name = (
        "runtime/PandratorManager.exe"
        if os.name == "nt"
        else "runtime/pandrator-manager"
    )
    metadata = {
        "schema_version": 1,
        "product": "pandrator-manager",
        "version": version,
        "application_root": "app",
        "python": runtime_name,
        "runtime_kind": "native_launcher",
    }
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            _write_bytes(
                archive,
                "pandrator-release.json",
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
            _write_bytes(
                archive,
                "app/.pandrator-manager-runtime",
                (version + "\n").encode("utf-8"),
            )
            _write_bytes(
                archive,
                runtime_name,
                bootstrap.read_bytes(),
                executable=True,
            )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        json.dumps(
            {
                "artifact": str(output),
                "sha256": sha256_file(output),
                "size_bytes": output.stat().st_size,
                "version": version,
                "runtime_kind": "native_launcher",
                "authenticode_signed": False if os.name == "nt" else None,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
