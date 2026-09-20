"""Load and validate the manager's mirrored audio.cpp package inventory.

The inventory is a build-time, dependency-free data file.  This module keeps
the validation rules next to the manager projection so an invalid or merely
catalogued upstream row cannot become an installable model by accident.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

INVENTORY_FILENAME = "audio_cpp_inventory.json"
PACKAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
HUGGINGFACE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SPEECH_TASKS = frozenset({"tts", "clone", "design", "vdes"})
CATALOGUE_ONLY_FAMILIES = frozenset({"minimax_h3", "vevo2"})


class InventoryError(ValueError):
    """The mirrored inventory does not satisfy its install contract."""


@dataclass(frozen=True, slots=True)
class InventoryManifestFile:
    source_path: str
    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class InventoryPackage:
    id: str
    family: str
    label: str
    format: str
    precision: str | None
    files: tuple[str, ...]
    target_directory: str
    strip_prefix: str
    download_kind: str
    repository: str
    revision: str
    gated: bool
    availability: str | dict[str, Any]
    manifest: tuple[InventoryManifestFile, ...]
    task: str

    @property
    def download_files(self) -> tuple[str, ...]:
        """Remote paths to pass to model-manager's exact-file downloader."""

        return tuple(item.source_path for item in self.manifest)


def _sequence(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise InventoryError(f"Inventory {field} must be a list.")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InventoryError(f"Inventory {field} must be a non-empty string.")
    return value


def _safe_relative(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise InventoryError(f"Inventory {field} must be a relative POSIX path.")
    if not value:
        if allow_empty:
            return ""
        raise InventoryError(f"Inventory {field} must be a relative POSIX path.")
    if "\\" in value or "\x00" in value or re.match(r"^[A-Za-z]:", value):
        raise InventoryError(f"Inventory {field} must not contain backslashes.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise InventoryError(f"Inventory {field} must not escape its root.")
    return value


def _availability_is_available(value: Any) -> bool:
    if isinstance(value, str):
        return value.casefold() == "available"
    if isinstance(value, dict):
        status = value.get("status")
        return (
            isinstance(status, str)
            and status.casefold() == "available"
        ) or (
            value.get("verified") is True
            and not value.get("missing_files")
            and value.get("unsupported_download") is not True
        )
    return False


def _family_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    families = _sequence(payload.get("families"), "families")
    result: dict[str, dict[str, Any]] = {}
    for row in families:
        if not isinstance(row, dict):
            raise InventoryError("Each inventory family must be an object.")
        family_id = _text(row.get("id"), "family.id")
        if family_id in result:
            raise InventoryError(f"Duplicate inventory family {family_id!r}.")
        tasks = _sequence(row.get("tasks"), f"family {family_id}.tasks")
        if any(not isinstance(task, str) or not task for task in tasks):
            raise InventoryError(f"Inventory family {family_id!r} has invalid tasks.")
        result[family_id] = row
    return result


def _infer_task(package_id: str, family_id: str, tasks: set[str]) -> str:
    if tasks and tasks <= {"design", "vdes"}:
        return "vdes"
    lowered_id = package_id.casefold()
    if (
        "voicedesign" in lowered_id
        or "voice_design" in lowered_id
        or family_id == "moss_voicegen"
    ):
        return "vdes"
    if (tasks - {"clone"}) and not (tasks & {"tts", "design", "vdes"}):
        return "clon"
    if "clone" in tasks and not (tasks & {"tts", "design", "vdes"}):
        return "clon"
    if family_id == "chatterbox" or (
        "chatterbox" in lowered_id and "turbo" not in lowered_id
    ):
        return "clon"
    return "tts"


def _manifest_rows(
    package_id: str,
    raw_manifest: Any,
    strip_prefix: str,
) -> tuple[InventoryManifestFile, ...]:
    rows = _sequence(raw_manifest, f"package {package_id}.manifest")
    if not rows:
        raise InventoryError(f"Package {package_id!r} has no manifest files.")
    result: list[InventoryManifestFile] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise InventoryError(f"Package {package_id!r} manifest row {index} is invalid.")
        source_path = _safe_relative(
            row.get("source_path"), f"package {package_id}.manifest.source_path"
        )
        path = _safe_relative(row.get("path"), f"package {package_id}.manifest.path")
        if strip_prefix:
            prefix = f"{strip_prefix}/"
            if not source_path.startswith(prefix):
                raise InventoryError(
                    f"Package {package_id!r} manifest path is outside strip_prefix."
                )
            expected_path = source_path[len(prefix) :]
        else:
            expected_path = source_path
        if path != expected_path:
            raise InventoryError(
                f"Package {package_id!r} manifest path does not match strip_prefix."
            )
        sha256 = row.get("sha256")
        if not isinstance(sha256, str) or SHA256_RE.fullmatch(sha256) is None:
            raise InventoryError(f"Package {package_id!r} has an invalid SHA-256 digest.")
        size = row.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InventoryError(f"Package {package_id!r} has an invalid manifest size.")
        result.append(InventoryManifestFile(source_path, path, sha256, size))
    return tuple(result)


def _parse_package(
    row: Any,
    families: dict[str, dict[str, Any]],
) -> InventoryPackage:
    if not isinstance(row, dict):
        raise InventoryError("Each inventory package must be an object.")
    package_id = _text(row.get("id"), "package.id")
    if PACKAGE_ID_RE.fullmatch(package_id) is None:
        raise InventoryError(f"Package {package_id!r} has an unsafe id.")
    family = _text(row.get("family"), f"package {package_id}.family")
    family_row = families.get(family)
    if family_row is None:
        raise InventoryError(f"Package {package_id!r} references an unknown family.")
    tasks = {str(task).casefold() for task in family_row["tasks"]}
    label = row.get("label", package_id)
    if not isinstance(label, str) or not label:
        raise InventoryError(f"Package {package_id!r} has an invalid label.")
    package_format = _text(row.get("format"), f"package {package_id}.format").casefold()
    precision = row.get("precision")
    if precision is not None and not isinstance(precision, str):
        raise InventoryError(f"Package {package_id!r} has an invalid precision.")
    raw_files = _sequence(row.get("files"), f"package {package_id}.files")
    files = tuple(_safe_relative(item, f"package {package_id}.files") for item in raw_files)
    if len(files) != len(set(files)):
        raise InventoryError(f"Package {package_id!r} contains duplicate files.")
    target_directory = _safe_relative(
        row.get("target_directory"), f"package {package_id}.target_directory"
    )
    strip_prefix = _safe_relative(
        row.get("strip_prefix", ""), f"package {package_id}.strip_prefix", allow_empty=True
    )
    download = row.get("download")
    if not isinstance(download, dict):
        raise InventoryError(f"Package {package_id!r} has no download object.")
    download_kind = _text(download.get("kind"), f"package {package_id}.download.kind")
    weight_manifest = row.get("weight_manifest")
    if weight_manifest is not None and not isinstance(weight_manifest, dict):
        raise InventoryError(f"Package {package_id!r} has an invalid weight manifest.")
    pinned = weight_manifest if isinstance(weight_manifest, dict) else {}
    repository = _text(
        pinned.get("repository", download.get("repo")),
        f"package {package_id}.download.repo",
    )
    if HUGGINGFACE_RE.fullmatch(repository) is None:
        raise InventoryError(f"Package {package_id!r} has an invalid Hugging Face repository.")
    revision = _text(
        pinned.get("revision", download.get("revision")),
        f"package {package_id}.download.revision",
    )
    if COMMIT_RE.fullmatch(revision) is None:
        raise InventoryError(f"Package {package_id!r} is not pinned to an immutable commit.")
    availability = row.get("availability")
    if isinstance(availability, dict) and "gated" in availability:
        gated = download.get("gated", False) or availability["gated"]
    else:
        gated = download.get("gated", False)
    if not isinstance(gated, bool):
        raise InventoryError(f"Package {package_id!r} has an invalid gated flag.")
    if not isinstance(availability, (str, dict)):
        raise InventoryError(f"Package {package_id!r} has invalid availability.")
    raw_manifest = row.get("manifest")
    if raw_manifest is None and isinstance(weight_manifest, dict):
        raw_manifest = weight_manifest.get("files")
    manifest = _manifest_rows(package_id, raw_manifest, strip_prefix)
    manifest_paths = tuple(item.path for item in manifest)
    source_paths = tuple(item.source_path for item in manifest)
    if files not in {manifest_paths, source_paths}:
        raise InventoryError(f"Package {package_id!r}.files does not match its manifest.")
    if package_format != "gguf":
        raise InventoryError(f"Package {package_id!r} is not a GGUF package.")
    gguf_files = tuple(path for path in manifest_paths if path.casefold().endswith(".gguf"))
    if not gguf_files:
        raise InventoryError(f"Package {package_id!r} has no GGUF file.")
    if len(gguf_files) > 1:
        raise InventoryError(f"Package {package_id!r} contains multiple GGUF files.")
    if package_id.startswith("dots_tts_edit_"):
        raise InventoryError(f"Package {package_id!r} is an editing variant, catalogued only.")
    if not tasks & SPEECH_TASKS or family in CATALOGUE_ONLY_FAMILIES:
        raise InventoryError(f"Package {package_id!r} is not a normal speech package.")
    if not _availability_is_available(availability) or gated:
        raise InventoryError(f"Package {package_id!r} is not available for installation.")
    return InventoryPackage(
        id=package_id,
        family=family,
        label=label,
        format=package_format,
        precision=precision,
        files=manifest_paths,
        target_directory=target_directory,
        strip_prefix=strip_prefix,
        download_kind=download_kind,
        repository=repository,
        revision=revision,
        gated=gated,
        availability=availability,
        manifest=manifest,
        task=_infer_task(package_id, family, tasks),
    )


def load_audio_cpp_inventory(path: str | Path | None = None) -> dict[str, Any]:
    """Read the raw mirrored inventory, without importing application modules."""

    selected = Path(path) if path is not None else Path(__file__).with_name(INVENTORY_FILENAME)
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError(f"Could not read audio.cpp inventory {selected}.") from error
    if not isinstance(payload, dict):
        raise InventoryError("The audio.cpp inventory root must be an object.")
    return payload


@lru_cache(maxsize=1)
def inventory() -> dict[str, Any]:
    """Return the packaged raw inventory for manager presentation callers."""

    return load_audio_cpp_inventory()


@lru_cache(maxsize=1)
def curation() -> dict[str, Any]:
    """Return the packaged reviewed metadata without importing the app."""

    selected = Path(__file__).with_name("audio_cpp_curation.json")
    try:
        payload = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InventoryError(f"Could not read audio.cpp curation {selected}.") from error
    if not isinstance(payload, dict):
        raise InventoryError("The audio.cpp curation root must be an object.")
    return payload


def validated_inventory_packages(path: str | Path | None = None) -> tuple[InventoryPackage, ...]:
    """Return only package rows that satisfy the install eligibility contract."""

    payload = load_audio_cpp_inventory(path)
    families = _family_rows(payload)
    rows = _sequence(payload.get("packages"), "packages")
    result: list[InventoryPackage] = []
    seen: set[str] = set()
    for row in rows:
        try:
            package = _parse_package(row, families)
        except InventoryError:
            continue
        if package.id in seen:
            continue
        seen.add(package.id)
        result.append(package)
    return tuple(result)


def load_audio_cpp_packages(path: str | Path | None = None) -> tuple[Any, ...]:
    """Project eligible inventory rows into manager model packages."""

    from .components.audiocpp import AudioCppModelPackage

    packages: list[AudioCppModelPackage] = []
    for package in validated_inventory_packages(path):
        language_options = None
        if package.family == "pocket_tts" and "/" in package.target_directory:
            language = package.target_directory.rsplit("/", 1)[-1]
            language_options = {"language": language}
        packages.append(
            AudioCppModelPackage(
                id=package.id,
                family=package.family,
                target_directory=package.target_directory,
                files=package.files,
                sha256=tuple(item.sha256 for item in package.manifest),
                task=package.task,
                load_options=language_options,
                session_options=language_options,
                label=package.label,
                precision=package.precision,
                download_kind=package.download_kind,
                repository=package.repository,
                revision=package.revision,
                download_files=package.download_files,
                strip_prefix=package.strip_prefix,
            )
        )
    return tuple(packages)


# Concise aliases for callers/tests that want the raw or validated view.
load_inventory = load_audio_cpp_inventory
inventory_packages = validated_inventory_packages
eligible_packages = validated_inventory_packages
