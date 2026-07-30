#!/usr/bin/env python3
"""Build one sorted SHA256SUMS manifest for public release assets."""

from __future__ import annotations

import argparse
import hashlib
import os
from collections.abc import Iterable
from pathlib import Path

_EXCLUDED_NAMES = frozenset({"SHA256SUMS", "release-notes.md"})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def release_assets(directory: Path) -> tuple[Path, ...]:
    selected = []
    for path in directory.iterdir():
        if (
            path.name in _EXCLUDED_NAMES
            or path.name.endswith(".sha256")
            or path.is_symlink()
            or not path.is_file()
        ):
            continue
        selected.append(path)
    return tuple(sorted(selected, key=lambda item: item.name))


def checksum_manifest(artifacts: Iterable[Path]) -> str:
    raw_paths = [Path(path).expanduser() for path in artifacts]
    for path in raw_paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Release artifact is not a regular file: {path}")
    selected = sorted(
        (path.resolve(strict=True) for path in raw_paths),
        key=lambda item: item.name,
    )
    if not selected:
        raise ValueError("At least one release artifact is required.")
    names = [path.name for path in selected]
    if len(names) != len(set(names)):
        raise ValueError("Release artifact basenames must be unique.")
    return "".join(
        f"{sha256_file(path)}  {path.name}\n"
        for path in selected
    )


def write_checksum_manifest(
    artifacts: Iterable[Path],
    output: Path,
) -> Path:
    destination = output.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(
            checksum_manifest(artifacts),
            encoding="ascii",
            newline="\n",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create one SHA256SUMS manifest for release assets."
    )
    parser.add_argument(
        "artifacts",
        nargs="*",
        type=Path,
        help="Explicit release assets. Omit when using --directory.",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        help=(
            "Select regular files in this directory, excluding checksum "
            "sidecars and release-notes.md."
        ),
    )
    parser.add_argument("--output", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if bool(args.artifacts) == bool(args.directory):
        raise ValueError(
            "Provide either explicit artifacts or --directory, but not both."
        )
    if args.directory:
        directory = args.directory.expanduser().resolve(strict=True)
        artifacts = release_assets(directory)
        output = args.output or directory / "SHA256SUMS"
    else:
        artifacts = tuple(args.artifacts)
        output = args.output or Path("SHA256SUMS")
    destination = write_checksum_manifest(artifacts, output)
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
