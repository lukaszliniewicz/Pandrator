#!/usr/bin/env python3
"""Build and locally smoke-test the Qt-free native manager bootstrap."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterator


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Freeze the Qt-free manager from one already-built wheel. If no "
            "wheel is supplied, a temporary wheel is built first."
        )
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--wheel", type=Path)
    source.add_argument(
        "--wheel-dir",
        type=Path,
        help="Directory that must contain exactly one pandrator-manager wheel.",
    )
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_wheel(path: Path) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if (
        not resolved.is_file()
        or resolved.is_symlink()
        or resolved.suffix.casefold() != ".whl"
        or not resolved.name.startswith("pandrator_manager-")
    ):
        raise RuntimeError(f"Unsafe or unexpected manager wheel: {resolved}")
    return resolved


def _wheel_from_directory(path: Path) -> Path:
    directory = path.expanduser().resolve(strict=True)
    if not directory.is_dir() or directory.is_symlink():
        raise RuntimeError(f"Unsafe or missing wheel directory: {directory}")
    wheels = sorted(directory.glob("pandrator_manager-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(
            f"Expected exactly one pandrator-manager wheel in {directory}; "
            f"found {len(wheels)}."
        )
    return _regular_wheel(wheels[0])


def _stage_wheel_source(repo_root: Path, destination: Path) -> Path:
    """Copy manager sources without checkout-local build residue."""

    source = (repo_root / "pandrator_manager").resolve(strict=True)
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError(f"Unsafe or missing manager source: {source}")
    for candidate in source.rglob("*"):
        if candidate.is_symlink():
            raise RuntimeError(
                f"Manager wheel source contains a symlink: {candidate}"
            )
    source_root = source.resolve(strict=True)

    def ignored(directory: str, names: list[str]) -> set[str]:
        current = Path(directory).resolve(strict=True)
        excluded = {
            name
            for name in names
            if name == "__pycache__"
            or name.endswith((".pyc", ".pyo", ".egg-info"))
        }
        if current == source_root:
            excluded.update(
                name
                for name in names
                if name
                in {
                    "build",
                    "dist",
                    ".pytest_cache",
                    ".ruff_cache",
                }
            )
        return excluded

    shutil.copytree(source, destination, ignore=ignored)
    return destination.resolve(strict=True)


def _build_temporary_wheel(repo_root: Path, destination: Path) -> Path:
    source = _stage_wheel_source(
        repo_root,
        destination.parent / "source",
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(destination),
            str(source),
        ],
        cwd=repo_root,
        check=True,
    )
    return _wheel_from_directory(destination)


def _extract_wheel(wheel: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve(strict=True)
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            parts = Path(member.filename.replace("\\", "/")).parts
            if (
                not parts
                or Path(member.filename).is_absolute()
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise RuntimeError(
                    f"Wheel contains an unsafe member: {member.filename!r}"
                )
            mode = member.external_attr >> 16
            if mode and stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise RuntimeError(
                    f"Wheel contains a non-regular member: {member.filename!r}"
                )
            target = destination.joinpath(*parts)
            resolved = target.resolve(strict=False)
            if not resolved.is_relative_to(destination_root):
                raise RuntimeError(
                    f"Wheel member escapes the extraction root: {member.filename!r}"
                )
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (tuple, list, set)):
        for item in value:
            yield from _iter_strings(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)


def _verify_wheel_provenance(
    repo_root: Path,
    wheel_root: Path,
) -> None:
    toc_path = repo_root / "build" / "pandrator_manager_bootstrap" / "Analysis-00.toc"
    try:
        analysis = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as error:
        raise RuntimeError("Could not inspect PyInstaller module provenance.") from error
    wheel_package = (wheel_root / "pandrator_manager").resolve(strict=True)
    source_package = (repo_root / "pandrator_manager").resolve(strict=True)
    manager_sources: list[Path] = []
    for value in _iter_strings(analysis):
        if not value.casefold().endswith(".py"):
            continue
        candidate = Path(value).resolve(strict=False)
        if "pandrator_manager" not in {
            part.casefold() for part in candidate.parts
        }:
            continue
        manager_sources.append(candidate)
    if not manager_sources or not any(
        path.is_relative_to(wheel_package) for path in manager_sources
    ):
        raise RuntimeError("PyInstaller did not analyze the supplied manager wheel.")
    leaked = [
        path
        for path in manager_sources
        if path.is_relative_to(source_package)
        and not path.is_relative_to(wheel_package)
    ]
    if leaked:
        raise RuntimeError(
            "PyInstaller analyzed manager source from the checkout instead of "
            f"the supplied wheel: {leaked[0]}"
        )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    spec = repo_root / "pandrator_manager_bootstrap.spec"
    with tempfile.TemporaryDirectory(prefix="pandrator-manager-wheel-build-") as raw:
        temporary = Path(raw).resolve(strict=True)
        if args.wheel is not None:
            wheel = _regular_wheel(
                args.wheel
                if args.wheel.is_absolute()
                else repo_root / args.wheel
            )
        elif args.wheel_dir is not None:
            wheel = _wheel_from_directory(
                args.wheel_dir
                if args.wheel_dir.is_absolute()
                else repo_root / args.wheel_dir
            )
        else:
            wheel = _build_temporary_wheel(repo_root, temporary / "wheel")
        wheel_digest = sha256_file(wheel)
        wheel_root = temporary / "site"
        _extract_wheel(wheel, wheel_root)
        entrypoint = temporary / "pandrator_manager_bootstrap.py"
        entrypoint.write_text(
            "from pandrator_manager.launcher import main\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    raise SystemExit(main())\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment["PANDRATOR_MANAGER_WHEEL_ROOT"] = str(wheel_root)
        environment["PANDRATOR_MANAGER_BOOTSTRAP_ENTRY"] = str(entrypoint)
        environment["PYINSTALLER_CONFIG_DIR"] = str(
            temporary / "pyinstaller-config"
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--clean",
                "--noconfirm",
                str(spec),
            ],
            cwd=repo_root,
            env=environment,
            check=True,
        )
        _verify_wheel_provenance(repo_root, wheel_root)
        suffix = ".exe" if sys.platform == "win32" else ""
        executable = repo_root / "dist" / f"PandratorManagerBootstrap{suffix}"
        result = subprocess.run(
            [str(executable), "self-check"],
            cwd=repo_root,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        report = json.loads(result.stdout)
        if not report.get("ok") or not report.get("frozen"):
            raise RuntimeError("Packaged manager bootstrap self-check failed.")
        digest = sha256_file(executable)
    print(
        json.dumps(
            {
                "artifact": str(executable),
                "sha256": digest,
                "source_wheel": wheel.name,
                "source_wheel_sha256": wheel_digest,
                "wheel_embedded": True,
                "authenticode_signed": False if sys.platform == "win32" else None,
                "self_check": report,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
