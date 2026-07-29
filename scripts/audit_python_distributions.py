"""Fail closed when Python release archives contain unexpected material."""

from __future__ import annotations

import argparse
import re
import tarfile
import tomllib
import zipfile
from collections import Counter
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def project_identity(project_dir: Path) -> tuple[str, str]:
    metadata = tomllib.loads((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    project = metadata["project"]
    return canonical_name(project["name"]), str(project["version"])


def archive_members(path: Path) -> tuple[list[str], bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            metadata_names = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError(
                    f"{path.name}: expected one wheel METADATA file, found "
                    f"{len(metadata_names)}"
                )
            return names, archive.read(metadata_names[0])

    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            metadata_members = [
                member
                for member in members
                if (
                    PurePosixPath(member.name).name == "PKG-INFO"
                    and len(PurePosixPath(member.name).parts) == 2
                )
            ]
            if len(metadata_members) != 1:
                raise ValueError(
                    f"{path.name}: expected one sdist PKG-INFO file, found "
                    f"{len(metadata_members)}"
                )
            extracted = archive.extractfile(metadata_members[0])
            if extracted is None:
                raise ValueError(f"{path.name}: could not read PKG-INFO")
            return names, extracted.read()

    raise ValueError(f"{path.name}: expected a .whl or .tar.gz distribution")


def validate_members(path: Path, names: list[str]) -> None:
    rejected: list[str] = []
    internal_architecture_files = {
        "backend_architecture.md",
        "frontend_architecture.md",
        "installer_manager_architecture.md",
    }

    for raw_name in names:
        normalized = raw_name.replace("\\", "/")
        member = PurePosixPath(normalized)
        lowered_parts = tuple(part.lower() for part in member.parts)
        lowered_name = member.name.lower()

        unsafe = (
            member.is_absolute()
            or ".." in member.parts
            or "__pycache__" in lowered_parts
            or lowered_name.endswith((".pyc", ".pyo"))
        )
        internal_markdown = lowered_name.endswith(".md") and (
            "plan" in lowered_name
            or lowered_name in internal_architecture_files
            or any(
                lowered_parts[index : index + 2] == ("docs", "qualification")
                for index in range(max(0, len(lowered_parts) - 1))
            )
        )
        if unsafe or internal_markdown:
            rejected.append(normalized)

    if rejected:
        preview = "\n  ".join(rejected[:20])
        remainder = len(rejected) - 20
        suffix = f"\n  ... and {remainder} more" if remainder > 0 else ""
        raise ValueError(f"{path.name}: rejected archive members:\n  {preview}{suffix}")


def distribution_identity(path: Path) -> tuple[str, str]:
    names, metadata_bytes = archive_members(path)
    validate_members(path, names)
    metadata = BytesParser().parsebytes(metadata_bytes)
    name = metadata.get("Name")
    version = metadata.get("Version")
    if not name or not version:
        raise ValueError(f"{path.name}: package metadata has no Name or Version")
    return canonical_name(name), version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project",
        action="append",
        required=True,
        type=Path,
        help="Project directory whose pyproject.toml defines an expected package.",
    )
    parser.add_argument("distributions", nargs="+", type=Path)
    args = parser.parse_args()

    expected = {project_identity(path.resolve()) for path in args.project}
    if len(expected) != len(args.project):
        raise ValueError("project metadata contains a duplicate package identity")

    artifacts: Counter[tuple[str, str, str]] = Counter()
    observed_identities: set[tuple[str, str]] = set()
    for path in (item.resolve() for item in args.distributions):
        identity = distribution_identity(path)
        observed_identities.add(identity)
        kind = "wheel" if path.suffix == ".whl" else "sdist"
        artifacts[(*identity, kind)] += 1

    if observed_identities != expected:
        raise ValueError(
            f"distribution identities differ from project metadata: "
            f"expected={sorted(expected)!r}, observed={sorted(observed_identities)!r}"
        )

    expected_artifacts = {
        (*identity, kind) for identity in expected for kind in ("sdist", "wheel")
    }
    observed_artifacts = set(artifacts)
    duplicates = {key: count for key, count in artifacts.items() if count != 1}
    if observed_artifacts != expected_artifacts or duplicates:
        raise ValueError(
            f"expected one wheel and one sdist per project: "
            f"observed={sorted(artifacts.items())!r}"
        )

    print(
        "Verified release archives: "
        + ", ".join(f"{name} {version}" for name, version in sorted(expected))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
