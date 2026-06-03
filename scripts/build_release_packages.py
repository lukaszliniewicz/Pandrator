#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence


PACKAGING_LAYOUT_FILENAME = "packaging_layout.json"
INSTALLER_STATE_FILENAME = "installer_state.json"
KOKORO_ENV_NAME = "kokoro_api_server_installer"

DEFAULT_CONFIG_FLAGS = (
    "cuda_support",
    "xtts_support",
    "silero_support",
    "voxtral_support",
    "kokoro_support",
    "whisperx_support",
    "xtts_finetuning_support",
    "rvc_support",
    "voxcpm_support",
    "fishs2_support",
)

DEFAULT_SHARED_PATHS = (
    "Pandrator",
    "Subdub",
    "bin",
    "Calibre Portable",
    ".pixi-home",
    "cache",
    "envs/pandrator_installer",
    "config.json",
    INSTALLER_STATE_FILENAME,
    PACKAGING_LAYOUT_FILENAME,
)

DEFAULT_EXCLUDED_FILE_PREFIXES = (
    "pandrator_state.sqlite3",
)
ACTIVE_EXCLUDED_FILE_PREFIXES = tuple(DEFAULT_EXCLUDED_FILE_PREFIXES)

DEFAULT_COMPONENT_PATHS = {
    "xtts": ("xtts2_api",),
    "voxtral": ("voxtral-fastapi",),
    "kokoro": (
        "Kokoro-FastAPI",
        f"envs/{KOKORO_ENV_NAME}",
    ),
    "silero": ("envs/silero_api_server_installer",),
    "whisperx": ("envs/whisperx_installer",),
    "xtts_finetuning": (
        "easy_xtts_trainer",
        "envs/easy_xtts_trainer",
    ),
    "voxcpm": ("voxcpm_fastapi",),
    "fishs2": ("fishs2-cpp-fastapi",),
}

@dataclass(frozen=True)
class BlockDefinition:
    name: str
    source_root: Path
    include_paths: tuple[str, ...]
    required_markers: tuple[str, ...]
    config_overrides: Dict[str, bool]


@dataclass(frozen=True)
class PackageDefinition:
    key: str
    archive_name: str
    blocks: tuple[str, ...]


@dataclass(frozen=True)
class SourceProfile:
    name: str
    components: tuple[str, ...]
    required_markers: tuple[str, ...]


SOURCE_PROFILES = {
    "core": SourceProfile(
        name="core",
        components=(),
        required_markers=(
            "Pandrator/main.py",
            "envs/pandrator_installer/pixi.toml",
        ),
    ),
    "stack": SourceProfile(
        name="stack",
        components=("xtts", "whisperx", "xtts_finetuning", "rvc"),
        required_markers=(
            "Pandrator/main.py",
            "xtts2_api/run.bat",
            "envs/whisperx_installer/pixi.toml",
            "easy_xtts_trainer/requirements.txt",
        ),
    ),
    "kokoro": SourceProfile(
        name="kokoro",
        components=("kokoro",),
        required_markers=(
            "Pandrator/main.py",
            "Kokoro-FastAPI/api/src/main.py",
            f"envs/{KOKORO_ENV_NAME}/pixi.toml",
        ),
    ),
    "voxtral": SourceProfile(
        name="voxtral",
        components=("voxtral",),
        required_markers=(
            "Pandrator/main.py",
            "voxtral-fastapi/run.ps1",
        ),
    ),
    "voxcpm": SourceProfile(
        name="voxcpm",
        components=("voxcpm",),
        required_markers=(
            "Pandrator/main.py",
            "voxcpm_fastapi/run.bat",
        ),
    ),
    "fishs2": SourceProfile(
        name="fishs2",
        components=("fishs2",),
        required_markers=(
            "Pandrator/main.py",
            "fishs2-cpp-fastapi/run.bat",
        ),
    ),
}

COMPONENT_CONFIG_FLAG_BY_NAME = {
    "xtts": "xtts_support",
    "xtts_cpu": "xtts_support",
    "silero": "silero_support",
    "voxtral": "voxtral_support",
    "kokoro": "kokoro_support",
    "rvc": "rvc_support",
    "whisperx": "whisperx_support",
    "xtts_finetuning": "xtts_finetuning_support",
    "voxcpm": "voxcpm_support",
    "fishs2": "fishs2_support",
}

PACKAGE_DEFINITIONS = {
    "kokoro": PackageDefinition(
        key="kokoro",
        archive_name="Pandrator-Kokoro",
        blocks=("core", "kokoro"),
    ),
    "xtts_stack": PackageDefinition(
        key="xtts_stack",
        archive_name="Pandrator-XTTS-WhisperX-FineTuning-RVC",
        blocks=("core_rvc", "xtts_stack"),
    ),
    "voxtral": PackageDefinition(
        key="voxtral",
        archive_name="Pandrator-Voxtral",
        blocks=("core", "voxtral"),
    ),
    "voxcpm": PackageDefinition(
        key="voxcpm",
        archive_name="Pandrator-VoxCPM",
        blocks=("core", "voxcpm"),
    ),
    "fishs2": PackageDefinition(
        key="fishs2",
        archive_name="Pandrator-FishS2",
        blocks=("core", "fishs2"),
    ),
    "voxtral_with_rest": PackageDefinition(
        key="voxtral_with_rest",
        archive_name="Pandrator-Voxtral-XTTS-WhisperX-FineTuning-RVC",
        blocks=("core_rvc", "xtts_stack", "voxtral"),
    ),
}

DEFAULT_PACKAGE_ORDER = (
    "kokoro",
    "xtts_stack",
    "voxtral",
    "voxcpm",
    "fishs2",
    "voxtral_with_rest",
)

PACKAGE_KEY_ALIASES = {
    "kokoro": "kokoro",
    "xtts": "xtts_stack",
    "xtts_stack": "xtts_stack",
    "stack": "xtts_stack",
    "voxtral": "voxtral",
    "voxcpm": "voxcpm",
    "fishs2": "fishs2",
    "fish": "fishs2",
    "voxtral_with_rest": "voxtral_with_rest",
    "voxtral_rest": "voxtral_with_rest",
    "voxtral_plus_rest": "voxtral_with_rest",
}

BLOCK_SOURCE_BY_NAME = {
    "core": "core",
    "core_rvc": "stack",
    "xtts_stack": "stack",
    "kokoro": "kokoro",
    "voxtral": "voxtral",
    "voxcpm": "voxcpm",
    "fishs2": "fishs2",
}


def normalize_relative_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _remove_tree_onerror(func, path, exc_info):
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def remove_tree(path: Path, retries: int = 3, delay_seconds: float = 1.0) -> None:
    if not path.exists():
        return

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            shutil.rmtree(path, onerror=_remove_tree_onerror)
            return
        except PermissionError as error:
            last_error = error
            if attempt >= retries:
                break
            time.sleep(delay_seconds)

    if last_error is not None:
        raise last_error


def resolve_cli_path(path_value: str | None, base_dir: Path) -> str | None:
    if path_value is None:
        return None

    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base_dir / path

    return str(path.resolve())


def resolve_path_from_base(path_value: str, base_dir: Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def parse_selected_package_keys(only_value: str, skip_voxtral_with_rest: bool) -> tuple[str, ...]:
    default_keys = [key for key in DEFAULT_PACKAGE_ORDER if key in PACKAGE_DEFINITIONS]
    if skip_voxtral_with_rest and "voxtral_with_rest" in default_keys:
        default_keys.remove("voxtral_with_rest")

    raw_value = str(only_value or "").strip().lower()
    if not raw_value or raw_value == "all":
        return tuple(default_keys)

    selected_keys: list[str] = []
    for token in raw_value.split(","):
        normalized_token = token.strip().lower().replace("-", "_")
        if not normalized_token:
            continue

        if normalized_token == "all":
            selected_keys.extend(default_keys)
            continue

        mapped_key = PACKAGE_KEY_ALIASES.get(normalized_token)
        if mapped_key is None:
            allowed = ", ".join(sorted(set(PACKAGE_KEY_ALIASES.keys()) | {"all"}))
            raise RuntimeError(
                f"Unsupported package selector '{token}'. Allowed values: {allowed}."
            )

        selected_keys.append(mapped_key)

    deduped_keys: list[str] = []
    for key in selected_keys:
        if key not in deduped_keys:
            deduped_keys.append(key)

    if not deduped_keys:
        raise RuntimeError("No valid packages were selected.")

    return tuple(deduped_keys)


def collect_required_blocks(packages: Sequence[PackageDefinition]) -> tuple[str, ...]:
    required_blocks: list[str] = []
    for package in packages:
        for block_name in package.blocks:
            if block_name not in required_blocks:
                required_blocks.append(block_name)
    return tuple(required_blocks)


def collect_required_sources(required_blocks: Sequence[str]) -> tuple[str, ...]:
    required_sources: list[str] = []
    for block_name in required_blocks:
        source_name = BLOCK_SOURCE_BY_NAME.get(block_name)
        if source_name is None:
            raise RuntimeError(f"No source mapping configured for block '{block_name}'.")
        if source_name not in required_sources:
            required_sources.append(source_name)
    return tuple(required_sources)


def apply_core_source_fallback(
    source_arguments: Dict[str, str | None],
    required_source_names: Sequence[str],
) -> None:
    if "core" not in required_source_names or source_arguments.get("core"):
        return

    required_source_set = set(required_source_names)
    if required_source_set.issubset({"core", "kokoro"}) and source_arguments.get("kokoro"):
        source_arguments["core"] = source_arguments["kokoro"]
        return

    if required_source_set.issubset({"core", "voxtral"}) and source_arguments.get("voxtral"):
        source_arguments["core"] = source_arguments["voxtral"]
        return

    if required_source_set.issubset({"core", "voxcpm"}) and source_arguments.get("voxcpm"):
        source_arguments["core"] = source_arguments["voxcpm"]
        return

    if required_source_set.issubset({"core", "fishs2"}) and source_arguments.get("fishs2"):
        source_arguments["core"] = source_arguments["fishs2"]


def parse_path_list(raw_values: object, fallback: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(raw_values, list):
        return tuple(normalize_relative_path(value) for value in fallback)

    normalized_values: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            continue
        normalized_value = normalize_relative_path(raw_value)
        if normalized_value:
            normalized_values.append(normalized_value)

    if not normalized_values:
        return tuple(normalize_relative_path(value) for value in fallback)

    return tuple(dict.fromkeys(normalized_values))


def parse_config_flags(raw_values: object) -> tuple[str, ...]:
    if not isinstance(raw_values, list):
        return tuple(DEFAULT_CONFIG_FLAGS)

    flags = [value for value in raw_values if isinstance(value, str) and value.strip()]
    if not flags:
        return tuple(DEFAULT_CONFIG_FLAGS)

    return tuple(dict.fromkeys(flags))


def load_json_file(path: Path) -> Mapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object in {path}")
    return data


def load_packaging_layout(source_root: Path) -> Dict[str, object]:
    layout = {
        "config_flags": tuple(DEFAULT_CONFIG_FLAGS),
        "shared_paths": tuple(DEFAULT_SHARED_PATHS),
        "excluded_file_prefixes": tuple(DEFAULT_EXCLUDED_FILE_PREFIXES),
        "component_paths": {
            key: tuple(values)
            for key, values in DEFAULT_COMPONENT_PATHS.items()
        },
    }

    layout_file = source_root / PACKAGING_LAYOUT_FILENAME
    if not layout_file.exists():
        return layout

    raw_layout = load_json_file(layout_file)
    layout["config_flags"] = parse_config_flags(raw_layout.get("config_flags"))
    layout["shared_paths"] = parse_path_list(raw_layout.get("shared_paths"), DEFAULT_SHARED_PATHS)
    layout["excluded_file_prefixes"] = parse_path_list(
        raw_layout.get("excluded_file_prefixes"),
        DEFAULT_EXCLUDED_FILE_PREFIXES,
    )

    raw_component_paths = raw_layout.get("component_paths")
    if isinstance(raw_component_paths, dict):
        component_paths: Dict[str, tuple[str, ...]] = {}
        for component_name, fallback_paths in DEFAULT_COMPONENT_PATHS.items():
            component_paths[component_name] = parse_path_list(
                raw_component_paths.get(component_name),
                fallback_paths,
            )
        layout["component_paths"] = component_paths

    return layout


def resolve_install_root(path_value: str) -> Path:
    source = Path(path_value).expanduser().resolve()
    if not source.exists():
        raise RuntimeError(f"Path does not exist: {source}")

    if (source / "config.json").exists() and (source / "envs").exists():
        return source

    nested = source / "Pandrator"
    if (nested / "config.json").exists() and (nested / "envs").exists():
        return nested

    raise RuntimeError(
        "Could not locate a Pandrator installation root in "
        f"{source}. Provide either the installation root or a parent directory that contains Pandrator/."
    )


def source_matches_profile(source_root: Path, profile: SourceProfile) -> bool:
    for marker in profile.required_markers:
        marker_path = source_root / Path(normalize_relative_path(marker))
        if not marker_path.exists():
            return False

    if not profile.components:
        return True

    config = load_install_config(source_root)
    for component in profile.components:
        config_flag = COMPONENT_CONFIG_FLAG_BY_NAME.get(component)
        if not config_flag:
            continue
        if not bool(config.get(config_flag, False)):
            return False

    return True


def run_headless_installer(
    installer_script: Path,
    python_executable: str,
    workspace: Path,
    components: Sequence[str],
) -> None:
    command = [
        python_executable,
        str(installer_script),
        "--headless-install",
        "--workspace",
        str(workspace),
    ]
    if components:
        command.extend(["--components", ",".join(components)])

    print(f"Running installer for workspace: {workspace}")
    subprocess.run(command, check=True, cwd=str(installer_script.parent))


def prepare_source_profile(
    profile: SourceProfile,
    sources_root: Path,
    installer_script: Path,
    python_executable: str,
    force_prepare: bool,
) -> Path:
    workspace = sources_root / profile.name
    install_root = workspace / "Pandrator"
    workspace.mkdir(parents=True, exist_ok=True)

    if force_prepare and install_root.exists():
        print(f"Removing existing prepared source: {install_root}")
        remove_tree(install_root)

    if install_root.exists() and source_matches_profile(install_root, profile):
        print(f"Reusing prepared source profile '{profile.name}': {install_root}")
        return install_root

    run_headless_installer(
        installer_script=installer_script,
        python_executable=python_executable,
        workspace=workspace,
        components=profile.components,
    )

    if not install_root.exists() or not source_matches_profile(install_root, profile):
        raise RuntimeError(
            f"Prepared source profile '{profile.name}' is incomplete at {install_root}."
        )

    return install_root


def prepare_missing_sources(
    source_arguments: Mapping[str, str | None],
    required_source_names: Sequence[str],
    sources_root: Path,
    installer_script: Path,
    python_executable: str,
    force_prepare: bool,
) -> Dict[str, Path]:
    prepared_sources: Dict[str, Path] = {}
    profile_lookup = {
        "core": SOURCE_PROFILES["core"],
        "stack": SOURCE_PROFILES["stack"],
        "kokoro": SOURCE_PROFILES["kokoro"],
        "voxtral": SOURCE_PROFILES["voxtral"],
        "voxcpm": SOURCE_PROFILES["voxcpm"],
        "fishs2": SOURCE_PROFILES["fishs2"],
    }

    for source_name in required_source_names:
        profile = profile_lookup.get(source_name)
        if profile is None:
            continue

        if source_arguments.get(source_name):
            continue

        prepared_sources[source_name] = prepare_source_profile(
            profile=profile,
            sources_root=sources_root,
            installer_script=installer_script,
            python_executable=python_executable,
            force_prepare=force_prepare,
        )

    return prepared_sources


def resolve_installer_executable(explicit_path: str | None, repo_root: Path) -> Path:
    if explicit_path:
        installer_path = Path(explicit_path).expanduser().resolve()
        if not installer_path.exists():
            raise RuntimeError(f"Installer executable not found: {installer_path}")
        return installer_path

    candidate_paths = (
        repo_root / "dist" / "PandratorInstaller.exe",
        repo_root / "dist" / "pandrator_installer_launcher.exe",
    )
    for candidate in candidate_paths:
        if candidate.exists():
            return candidate

    raise RuntimeError(
        "Could not find an installer executable. Pass --installer-exe or build one in dist/."
    )


def load_install_config(source_root: Path) -> Dict[str, object]:
    config_path = source_root / "config.json"
    if not config_path.exists():
        return {}
    raw_config = load_json_file(config_path)
    return dict(raw_config)


def iter_files_in_tree(root: Path) -> Iterable[Path]:
    if root.is_file():
        if should_exclude_file(root.name):
            return
        yield root
        return

    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not should_exclude_file(d)]
        dirs.sort()
        files.sort()
        current_path = Path(current_root)
        for filename in files:
            if should_exclude_file(filename):
                continue
            yield current_path / filename


def should_exclude_file(filename: str, excluded_prefixes: Sequence[str] | None = None) -> bool:
    if not filename:
        return False

    lowered = filename.lower()
    if lowered.endswith((".pyc", ".pyo", ".log")) or lowered == "__pycache__":
        return True

    prefixes = excluded_prefixes or ACTIVE_EXCLUDED_FILE_PREFIXES
    for prefix in prefixes:
        normalized_prefix = str(prefix or "").strip().lower()
        if not normalized_prefix:
            continue
        if lowered.startswith(normalized_prefix):
            return True
    return False


def calculate_block_signature(source_root: Path, include_paths: Sequence[str]) -> str:
    digest = hashlib.sha256()
    digest.update(str(source_root).encode("utf-8"))

    for include_path in sorted(dict.fromkeys(include_paths)):
        normalized = normalize_relative_path(include_path)
        source_entry = source_root / Path(normalized)
        digest.update(f"entry:{normalized}\n".encode("utf-8"))

        if not source_entry.exists():
            digest.update(b"missing\n")
            continue

        if source_entry.is_file():
            if should_exclude_file(source_entry.name):
                digest.update(b"excluded\n")
                continue
            stat = source_entry.stat()
            digest.update(
                f"file:{normalized}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8")
            )
            continue

        for file_path in iter_files_in_tree(source_entry):
            relative_file = file_path.relative_to(source_root).as_posix()
            stat = file_path.stat()
            digest.update(
                f"file:{relative_file}|{stat.st_size}|{stat.st_mtime_ns}\n".encode("utf-8")
            )

    return digest.hexdigest()


def hardlink_or_copy_file(source_file: Path, destination_file: Path, prefer_hardlinks: bool) -> None:
    destination_file.parent.mkdir(parents=True, exist_ok=True)

    if destination_file.exists():
        if destination_file.is_dir():
            remove_tree(destination_file)
        else:
            destination_file.unlink()

    if prefer_hardlinks:
        try:
            os.link(str(source_file), str(destination_file))
            return
        except OSError:
            pass

    shutil.copy2(source_file, destination_file)


def copy_directory_contents(source_dir: Path, destination_dir: Path, prefer_hardlinks: bool) -> None:
    destination_dir.mkdir(parents=True, exist_ok=True)

    for current_root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not should_exclude_file(d)]
        dirs.sort()
        files.sort()
        root_path = Path(current_root)
        relative_root = root_path.relative_to(source_dir)
        target_root = destination_dir / relative_root
        target_root.mkdir(parents=True, exist_ok=True)

        for filename in files:
            if should_exclude_file(filename):
                continue
            source_file = root_path / filename
            destination_file = target_root / filename
            hardlink_or_copy_file(source_file, destination_file, prefer_hardlinks)


def copy_relative_entries(
    source_root: Path,
    destination_root: Path,
    relative_paths: Sequence[str],
    prefer_hardlinks: bool,
) -> None:
    for relative_path in sorted(dict.fromkeys(relative_paths)):
        normalized = normalize_relative_path(relative_path)
        source_entry = source_root / Path(normalized)
        if not source_entry.exists():
            continue

        destination_entry = destination_root / Path(normalized)
        if source_entry.is_dir():
            if destination_entry.is_file():
                destination_entry.unlink()
            copy_directory_contents(source_entry, destination_entry, prefer_hardlinks)
            continue

        if should_exclude_file(source_entry.name):
            continue

        if destination_entry.is_dir():
            remove_tree(destination_entry)
        hardlink_or_copy_file(source_entry, destination_entry, prefer_hardlinks)


def ensure_required_markers(block: BlockDefinition) -> None:
    missing_markers = []
    for marker in block.required_markers:
        marker_path = block.source_root / Path(normalize_relative_path(marker))
        if not marker_path.exists():
            missing_markers.append(marker)

    if missing_markers:
        joined_markers = ", ".join(missing_markers)
        raise RuntimeError(
            f"Block '{block.name}' is missing required files in {block.source_root}: {joined_markers}"
        )


def load_block_metadata(metadata_path: Path) -> Dict[str, object]:
    if not metadata_path.exists():
        return {}

    try:
        raw_metadata = load_json_file(metadata_path)
        return dict(raw_metadata)
    except Exception:
        return {}


def ensure_block_cache(
    block: BlockDefinition,
    cache_root: Path,
    force_refresh: bool,
    prefer_hardlinks: bool,
) -> Path:
    block_cache_dir = cache_root / block.name
    metadata_path = block_cache_dir / ".block_metadata.json"
    signature = calculate_block_signature(block.source_root, block.include_paths)

    metadata = load_block_metadata(metadata_path)
    has_valid_cache = (
        not force_refresh
        and block_cache_dir.exists()
        and metadata.get("source_root") == str(block.source_root)
        and metadata.get("include_paths") == list(block.include_paths)
        and metadata.get("signature") == signature
    )

    if has_valid_cache:
        print(f"Reusing cached block: {block.name}")
        return block_cache_dir

    print(f"Refreshing block cache: {block.name}")
    if block_cache_dir.exists():
        remove_tree(block_cache_dir)
    block_cache_dir.mkdir(parents=True, exist_ok=True)

    copy_relative_entries(
        block.source_root,
        block_cache_dir,
        block.include_paths,
        prefer_hardlinks,
    )

    metadata_payload = {
        "source_root": str(block.source_root),
        "include_paths": list(block.include_paths),
        "signature": signature,
    }
    with metadata_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata_payload, handle, indent=2, sort_keys=True)

    return block_cache_dir


def overlay_block(cache_dir: Path, stage_root: Path, prefer_hardlinks: bool) -> None:
    for entry in sorted(cache_dir.iterdir(), key=lambda path: path.name.lower()):
        if entry.name == ".block_metadata.json":
            continue

        destination_entry = stage_root / entry.name
        if entry.is_dir():
            if destination_entry.is_file():
                destination_entry.unlink()
            copy_directory_contents(entry, destination_entry, prefer_hardlinks)
            continue

        if destination_entry.is_dir():
            remove_tree(destination_entry)
        hardlink_or_copy_file(entry, destination_entry, prefer_hardlinks)


def apply_config_flags(
    config_path: Path,
    config_flags: Sequence[str],
    package: PackageDefinition,
    block_definitions: Mapping[str, BlockDefinition],
) -> None:
    config_data: Dict[str, object] = {}
    if config_path.exists():
        try:
            loaded = load_json_file(config_path)
            config_data = dict(loaded)
        except Exception:
            config_data = {}

    for config_flag in config_flags:
        config_data[config_flag] = False

    for block_name in package.blocks:
        block = block_definitions[block_name]
        for key, value in block.config_overrides.items():
            config_data[key] = bool(value)

    if config_path.exists() and config_path.is_file():
        config_path.unlink()

    with config_path.open("w", encoding="utf-8") as handle:
        json.dump(config_data, handle, indent=2, sort_keys=True)


def create_zip_archive(source_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as archive:
        for file_path in iter_files_in_tree(source_dir):
            if not file_path.is_file():
                continue
            archive_name = file_path.relative_to(source_dir).as_posix()
            archive.write(file_path, archive_name)


def format_size(num_bytes: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    value = float(num_bytes)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build Pandrator zip packages from reusable local blocks. "
            "Each generated zip contains the installer executable and the Pandrator folder."
        )
    )
    parser.add_argument(
        "--release-root",
        default="package_release",
        help="Root working directory for release operations. The script changes CWD into this directory.",
    )
    parser.add_argument(
        "--only",
        default="all",
        help=(
            "Comma-separated package selector: all, kokoro, xtts_stack, voxtral, voxcpm, fishs2, voxtral_with_rest. "
            "Aliases: xtts, stack, voxtral_rest, fish."
        ),
    )
    parser.add_argument(
        "--core-source",
        default=None,
        help="Installation with core runtime (no RVC stack). Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--stack-source",
        default=None,
        help="Installation with XTTS + WhisperX + XTTS fine-tuning + RVC. Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--kokoro-source",
        default=None,
        help="Installation with Kokoro runtime ready. Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--voxtral-source",
        default=None,
        help="Installation with Voxtral runtime ready. Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--voxcpm-source",
        default=None,
        help="Installation with VoxCPM runtime ready. Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--fishs2-source",
        default=None,
        help="Installation with FishS2 runtime ready. Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--prepare-sources",
        action="store_true",
        help="Prepare missing source installs automatically using installer headless mode.",
    )
    parser.add_argument(
        "--sources-root",
        default=".release_sources",
        help="Root directory for auto-prepared source workspaces (relative to release root unless absolute).",
    )
    parser.add_argument(
        "--prepare-force",
        action="store_true",
        help="Reinstall prepared sources even when marker files already exist.",
    )
    parser.add_argument(
        "--installer-script",
        default=None,
        help="Path to pandrator_installer_launcher.py for source preparation.",
    )
    parser.add_argument(
        "--python-exe",
        default=sys.executable,
        help="Python executable used for running installer headless mode.",
    )
    parser.add_argument(
        "--installer-exe",
        default=None,
        help="Path to installer executable (defaults to dist/PandratorInstaller.exe).",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        default="release_packages",
        help="Output directory for generated zip archives (relative to release root unless absolute).",
    )
    parser.add_argument(
        "--cache-dir",
        default=".release_blocks",
        help="Directory used to cache reusable blocks (relative to release root unless absolute).",
    )
    parser.add_argument(
        "--staging-dir",
        default=".release_staging",
        help="Directory used for temporary package assembly (relative to release root unless absolute).",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Rebuild all blocks even if the cache signature matches.",
    )
    parser.add_argument(
        "--no-hardlinks",
        action="store_true",
        help="Copy files instead of hardlinking while caching/assembling blocks.",
    )
    parser.add_argument(
        "--skip-voxtral-with-rest",
        action="store_true",
        help="Skip creating the combined Voxtral + XTTS/WhisperX/Fine-tuning/RVC package.",
    )
    return parser.parse_args()


def main() -> int:
    global ACTIVE_EXCLUDED_FILE_PREFIXES
    args = parse_arguments()
    repo_root = Path(__file__).resolve().parents[1]

    invocation_cwd = Path.cwd().resolve()
    release_root = Path(resolve_cli_path(args.release_root, invocation_cwd) or "package_release")
    release_root.mkdir(parents=True, exist_ok=True)
    os.chdir(release_root)
    print(f"Using release root: {release_root}")

    selected_package_keys = parse_selected_package_keys(args.only, args.skip_voxtral_with_rest)
    packages = [PACKAGE_DEFINITIONS[key] for key in selected_package_keys]
    required_blocks = collect_required_blocks(packages)
    required_source_names = collect_required_sources(required_blocks)

    source_arguments: Dict[str, str | None] = {
        "core": resolve_cli_path(args.core_source, invocation_cwd),
        "stack": resolve_cli_path(args.stack_source, invocation_cwd),
        "kokoro": resolve_cli_path(args.kokoro_source, invocation_cwd),
        "voxtral": resolve_cli_path(args.voxtral_source, invocation_cwd),
        "voxcpm": resolve_cli_path(args.voxcpm_source, invocation_cwd),
        "fishs2": resolve_cli_path(args.fishs2_source, invocation_cwd),
    }
    apply_core_source_fallback(source_arguments, required_source_names)

    if args.prepare_sources:
        installer_script = (
            Path(resolve_cli_path(args.installer_script, invocation_cwd))
            if args.installer_script
            else (repo_root / "pandrator_installer_launcher.py").resolve()
        )
        if not installer_script.exists():
            raise RuntimeError(f"Installer script not found: {installer_script}")

        sources_root = resolve_path_from_base(args.sources_root, release_root)
        sources_root.mkdir(parents=True, exist_ok=True)

        required_sources_for_prepare = list(required_source_names)
        if "core" in required_sources_for_prepare and not source_arguments.get("core"):
            required_source_set = set(required_sources_for_prepare)
            if required_source_set.issubset({"core", "kokoro"}):
                required_sources_for_prepare = [
                    source_name for source_name in required_sources_for_prepare if source_name != "core"
                ]
            elif required_source_set.issubset({"core", "voxtral"}):
                required_sources_for_prepare = [
                    source_name for source_name in required_sources_for_prepare if source_name != "core"
                ]
            elif required_source_set.issubset({"core", "voxcpm"}):
                required_sources_for_prepare = [
                    source_name for source_name in required_sources_for_prepare if source_name != "core"
                ]
            elif required_source_set.issubset({"core", "fishs2"}):
                required_sources_for_prepare = [
                    source_name for source_name in required_sources_for_prepare if source_name != "core"
                ]

        prepared_sources = prepare_missing_sources(
            source_arguments=source_arguments,
            required_source_names=required_sources_for_prepare,
            sources_root=sources_root,
            installer_script=installer_script,
            python_executable=args.python_exe,
            force_prepare=args.prepare_force,
        )
        for source_name, prepared_path in prepared_sources.items():
            source_arguments[source_name] = str(prepared_path)

    apply_core_source_fallback(source_arguments, required_source_names)

    missing_sources = [
        source_name
        for source_name in required_source_names
        if not source_arguments.get(source_name)
    ]
    if missing_sources:
        missing_display = ", ".join(sorted(missing_sources))
        raise RuntimeError(
            "Missing source paths for: "
            f"{missing_display}. Provide the matching --*-source values or use --prepare-sources."
        )

    source_roots = {
        source_name: resolve_install_root(str(source_arguments[source_name]))
        for source_name in required_source_names
    }

    installer_executable = resolve_installer_executable(
        resolve_cli_path(args.installer_exe, invocation_cwd),
        repo_root,
    )

    layout_source = None
    for candidate_name in ("stack", "core", "kokoro", "voxtral", "voxcpm", "fishs2"):
        if candidate_name in source_roots:
            layout_source = source_roots[candidate_name]
            break
    if layout_source is None:
        raise RuntimeError("Could not determine a source for packaging layout metadata.")

    layout = load_packaging_layout(layout_source)

    config_flags = tuple(layout["config_flags"])
    shared_paths = tuple(layout["shared_paths"])
    ACTIVE_EXCLUDED_FILE_PREFIXES = tuple(
        layout.get("excluded_file_prefixes", DEFAULT_EXCLUDED_FILE_PREFIXES)
    )
    component_paths = dict(layout["component_paths"])

    stack_config = load_install_config(source_roots["stack"]) if "stack" in source_roots else {}
    cuda_support = bool(stack_config.get("cuda_support", True))

    xtts_stack_paths = tuple(
        dict.fromkeys(
            list(component_paths["xtts"])
            + list(component_paths["whisperx"])
            + list(component_paths["xtts_finetuning"])
        )
    )

    block_definitions: Dict[str, BlockDefinition] = {}

    if "core" in required_blocks:
        block_definitions["core"] = BlockDefinition(
            name="core",
            source_root=source_roots["core"],
            include_paths=shared_paths,
            required_markers=(
                "Pandrator/main.py",
                "envs/pandrator_installer/pixi.toml",
            ),
            config_overrides={},
        )

    if "core_rvc" in required_blocks:
        block_definitions["core_rvc"] = BlockDefinition(
            name="core_rvc",
            source_root=source_roots["stack"],
            include_paths=shared_paths,
            required_markers=(
                "Pandrator/main.py",
                "envs/pandrator_installer/pixi.toml",
            ),
            config_overrides={"rvc_support": True},
        )

    if "xtts_stack" in required_blocks:
        block_definitions["xtts_stack"] = BlockDefinition(
            name="xtts_stack",
            source_root=source_roots["stack"],
            include_paths=xtts_stack_paths,
            required_markers=(
                "xtts2_api/run.bat",
                "envs/whisperx_installer/pixi.toml",
                "easy_xtts_trainer/requirements.txt",
            ),
            config_overrides={
                "xtts_support": True,
                "cuda_support": cuda_support,
                "whisperx_support": True,
                "xtts_finetuning_support": True,
                "rvc_support": True,
            },
        )

    if "kokoro" in required_blocks:
        block_definitions["kokoro"] = BlockDefinition(
            name="kokoro",
            source_root=source_roots["kokoro"],
            include_paths=tuple(component_paths["kokoro"]),
            required_markers=(
                "Kokoro-FastAPI/api/src/main.py",
                f"envs/{KOKORO_ENV_NAME}/pixi.toml",
            ),
            config_overrides={"kokoro_support": True},
        )

    if "voxtral" in required_blocks:
        block_definitions["voxtral"] = BlockDefinition(
            name="voxtral",
            source_root=source_roots["voxtral"],
            include_paths=tuple(component_paths["voxtral"]),
            required_markers=("voxtral-fastapi/run.ps1",),
            config_overrides={"voxtral_support": True},
        )

    if "voxcpm" in required_blocks:
        block_definitions["voxcpm"] = BlockDefinition(
            name="voxcpm",
            source_root=source_roots["voxcpm"],
            include_paths=tuple(component_paths["voxcpm"]),
            required_markers=("voxcpm_fastapi/run.bat",),
            config_overrides={"voxcpm_support": True},
        )

    if "fishs2" in required_blocks:
        block_definitions["fishs2"] = BlockDefinition(
            name="fishs2",
            source_root=source_roots["fishs2"],
            include_paths=tuple(component_paths["fishs2"]),
            required_markers=("fishs2-cpp-fastapi/run.bat",),
            config_overrides={"fishs2_support": True},
        )

    for block_name in required_blocks:
        block = block_definitions.get(block_name)
        if block is None:
            raise RuntimeError(f"Block definition is missing for '{block_name}'.")
        ensure_required_markers(block)

    cache_root = resolve_path_from_base(args.cache_dir, release_root)
    staging_root = resolve_path_from_base(args.staging_dir, release_root)
    output_root = resolve_path_from_base(args.output_dir, release_root)
    prefer_hardlinks = not args.no_hardlinks

    cache_root.mkdir(parents=True, exist_ok=True)
    staging_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    print(f"Using output directory: {output_root}")

    cached_block_paths: Dict[str, Path] = {}
    for block_name in required_blocks:
        block = block_definitions[block_name]
        cached_block_paths[block_name] = ensure_block_cache(
            block,
            cache_root,
            force_refresh=args.force_refresh,
            prefer_hardlinks=prefer_hardlinks,
        )

    built_archives: list[tuple[str, Path, int]] = []
    for package in packages:
        print(f"Assembling package: {package.archive_name}")
        package_stage_root = staging_root / package.archive_name
        package_install_root = package_stage_root / "Pandrator"

        if package_stage_root.exists():
            remove_tree(package_stage_root)

        package_install_root.mkdir(parents=True, exist_ok=True)
        hardlink_or_copy_file(
            installer_executable,
            package_stage_root / installer_executable.name,
            prefer_hardlinks,
        )

        for block_name in package.blocks:
            overlay_block(cached_block_paths[block_name], package_install_root, prefer_hardlinks)

        config_path = package_install_root / "config.json"
        apply_config_flags(config_path, config_flags, package, block_definitions)

        archive_path = output_root / f"{package.archive_name}.zip"
        create_zip_archive(package_stage_root, archive_path)
        archive_size = archive_path.stat().st_size
        built_archives.append((package.archive_name, archive_path, archive_size))

    print("Built archives:")
    for package_name, archive_path, archive_size in built_archives:
        print(f"- {package_name}: {archive_path} ({format_size(archive_size)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
