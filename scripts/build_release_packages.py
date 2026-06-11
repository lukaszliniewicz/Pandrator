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

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pandrator_installer.catalog import (  # noqa: E402
    COMPONENTS,
    PACKAGING_COMPONENT_PATHS,
    PACKAGING_CONFIG_FLAGS,
    PACKAGING_EXCLUDED_FILE_PREFIXES,
    PACKAGING_LAYOUT_FILENAME,
    PACKAGING_SHARED_PATHS,
    RELEASE_COMPONENT_KEYS,
)

DEFAULT_CONFIG_FLAGS = PACKAGING_CONFIG_FLAGS
DEFAULT_SHARED_PATHS = PACKAGING_SHARED_PATHS
DEFAULT_EXCLUDED_FILE_PREFIXES = PACKAGING_EXCLUDED_FILE_PREFIXES
ACTIVE_EXCLUDED_FILE_PREFIXES = tuple(DEFAULT_EXCLUDED_FILE_PREFIXES)
DEFAULT_COMPONENT_PATHS = PACKAGING_COMPONENT_PATHS


@dataclass(frozen=True)
class BlockDefinition:
    name: str
    source_root: Path
    include_paths: tuple[str, ...]
    required_markers: tuple[str, ...]
    config_overrides: Dict[str, bool]


MODULES = {
    key: COMPONENTS[key]
    for key in (*RELEASE_COMPONENT_KEYS, "kokoro_cpu")
}

PRESETS = {
    "kokoro": ("kokoro",),
    "kokoro_cpu": ("kokoro_cpu",),
    "xtts_stack": ("xtts_finetuning", "rvc"),
    "xtts": ("xtts_finetuning", "rvc"),
    "stack": ("xtts_finetuning", "rvc"),
    "voxtral": ("voxtral",),
    "voxcpm": ("voxcpm",),
    "fishs2": ("fishs2",),
    "fish": ("fishs2",),
    "voxtral_with_rest": ("voxtral", "xtts_finetuning", "rvc"),
    "voxtral_rest": ("voxtral", "xtts_finetuning", "rvc"),
    "voxtral_plus_rest": ("voxtral", "xtts_finetuning", "rvc"),
}
ALL_MODULE_KEYS = RELEASE_COMPONENT_KEYS


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


def resolve_dependencies(selected_modules: Iterable[str]) -> tuple[str, ...]:
    resolved: list[str] = []

    def visit(module_key: str):
        if module_key in resolved:
            return
        if module_key not in MODULES:
            raise RuntimeError(f"Unknown module '{module_key}'.")

        module = MODULES[module_key]
        for dep in module.dependencies:
            visit(dep)
        resolved.append(module_key)

    for key in selected_modules:
        visit(key)

    return tuple(resolved)


def parse_selected_modules(only_value: str) -> tuple[str, ...]:
    raw_value = str(only_value or "").strip().lower()
    if not raw_value or raw_value == "all":
        return resolve_dependencies(ALL_MODULE_KEYS)

    selected_keys: set[str] = set()
    for token in raw_value.split(","):
        normalized_token = token.strip().lower().replace("-", "_")
        if not normalized_token:
            continue

        if normalized_token == "all":
            selected_keys.update(ALL_MODULE_KEYS)
            continue

        if normalized_token in PRESETS:
            selected_keys.update(PRESETS[normalized_token])
        elif normalized_token in MODULES:
            selected_keys.add(normalized_token)
        else:
            allowed = ", ".join(sorted(set(MODULES.keys()) | set(PRESETS.keys()) | {"all"}))
            raise RuntimeError(
                f"Unsupported module or preset '{token}'. Allowed values: {allowed}."
            )

    return resolve_dependencies(selected_keys)


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


def get_tailored_workspace_name(components: Sequence[str]) -> str:
    h = hashlib.sha256()
    for c in sorted(components):
        h.update(c.encode("utf-8"))
    return f"workspace_{h.hexdigest()[:12]}"


def prepare_tailored_source(
    components: Sequence[str],
    sources_root: Path,
    installer_script: Path,
    python_executable: str,
    force_prepare: bool,
) -> Path:
    workspace_name = get_tailored_workspace_name(components)
    workspace = sources_root / workspace_name
    install_root = workspace / "Pandrator"
    workspace.mkdir(parents=True, exist_ok=True)

    has_valid_source = not force_prepare and install_root.exists()
    if has_valid_source:
        core_markers = ("Pandrator/main.py", "envs/pandrator_installer/pixi.toml")
        for marker in core_markers:
            if not (install_root / Path(normalize_relative_path(marker))).exists():
                has_valid_source = False
                break

        if has_valid_source:
            for component in components:
                module = MODULES[component]
                for marker in module.markers:
                    if not (install_root / Path(normalize_relative_path(marker))).exists():
                        has_valid_source = False
                        break

    if has_valid_source:
        print(f"Reusing prepared source workspace: {install_root}")
        return install_root

    if install_root.exists():
        print(f"Removing existing incomplete source workspace: {install_root}")
        remove_tree(install_root)

    run_headless_installer(
        installer_script=installer_script,
        python_executable=python_executable,
        workspace=workspace,
        components=components,
    )

    # Double check markers
    core_markers = ("Pandrator/main.py", "envs/pandrator_installer/pixi.toml")
    for marker in core_markers:
        if not (install_root / Path(normalize_relative_path(marker))).exists():
            raise RuntimeError(f"Source workspace preparation failed. Missing core file: {marker}")

    for component in components:
        module = MODULES[component]
        for marker in module.markers:
            if not (install_root / Path(normalize_relative_path(marker))).exists():
                raise RuntimeError(f"Source workspace preparation failed. Missing component '{component}' file: {marker}")

    return install_root


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

    for block in block_definitions.values():
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
            "Build custom Pandrator zip packages from reusable local modules. "
            "The generated zip contains the installer executable and the tailored Pandrator folder."
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
            "Comma-separated list of components/presets to package: all, "
            "kokoro, kokoro_cpu, xtts_stack, voxtral, voxcpm, fishs2, voxtral_with_rest. "
            "Individual modules: kokoro, kokoro_cpu, voxtral, voxcpm, fishs2, xtts, silero, whisperx, xtts_finetuning, rvc."
        ),
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Path to pre-existing single installation root to package from. Required unless --prepare-sources is used.",
    )
    parser.add_argument(
        "--prepare-sources",
        action="store_true",
        help="Prepare a customized tailored source installation automatically using installer headless mode.",
    )
    parser.add_argument(
        "--sources-root",
        default=".release_sources",
        help="Root directory for auto-prepared tailored workspaces (relative to release root unless absolute).",
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
        "--name",
        default=None,
        help="Custom name for output zip file (defaults to dynamically generated module list).",
    )
    parser.add_argument(
        "--skip-voxtral-with-rest",
        action="store_true",
        help="Ignored (kept for backwards compatibility).",
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

    selected_modules = parse_selected_modules(args.only)
    print(f"Resolved modules to package: {', '.join(selected_modules) if selected_modules else 'None (Core Only)'}")

    installer_executable = resolve_installer_executable(
        resolve_cli_path(args.installer_exe, invocation_cwd),
        repo_root,
    )

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

        source_root = prepare_tailored_source(
            components=selected_modules,
            sources_root=sources_root,
            installer_script=installer_script,
            python_executable=args.python_exe,
            force_prepare=args.prepare_force,
        )
    else:
        if not args.source:
            raise RuntimeError("Provide either --source or use --prepare-sources.")
        source_root = resolve_install_root(str(resolve_cli_path(args.source, invocation_cwd)))

        for component in selected_modules:
            module = MODULES[component]
            for marker in module.markers:
                marker_path = source_root / Path(normalize_relative_path(marker))
                if not marker_path.exists():
                    raise RuntimeError(
                        f"Source root '{source_root}' is missing required file for module '{component}': {marker}"
                    )

    layout = load_packaging_layout(source_root)

    config_flags = tuple(layout["config_flags"])
    shared_paths = tuple(
        p for p in layout["shared_paths"]
        if normalize_relative_path(p) not in (".pixi-home", ".pixi-cache")
    )
    ACTIVE_EXCLUDED_FILE_PREFIXES = tuple(
        layout.get("excluded_file_prefixes", DEFAULT_EXCLUDED_FILE_PREFIXES)
    )
    component_paths = dict(layout["component_paths"])

    cuda_support = True
    kokoro_gpu_support = False
    if (source_root / "config.json").exists():
        try:
            config = load_install_config(source_root)
            cuda_support = bool(config.get("cuda_support", True))
            kokoro_gpu_support = bool(config.get("kokoro_gpu_support", False))
        except Exception:
            pass

    block_definitions: Dict[str, BlockDefinition] = {}
    block_definitions["core"] = BlockDefinition(
        name="core",
        source_root=source_root,
        include_paths=shared_paths,
        required_markers=(
            "Pandrator/main.py",
            "envs/pandrator_installer/pixi.toml",
        ),
        config_overrides={},
    )

    for component in selected_modules:
        module = MODULES[component]
        paths = module.paths
        if component in component_paths:
            paths = tuple(component_paths[component])
        elif component == "xtts" and "xtts" in component_paths:
            paths = tuple(component_paths["xtts"])
        elif component == "voxtral" and "voxtral" in component_paths:
            paths = tuple(component_paths["voxtral"])
        elif component in {"kokoro", "kokoro_cpu"} and "kokoro" in component_paths:
            paths = tuple(component_paths["kokoro"])
        elif component == "silero" and "silero" in component_paths:
            paths = tuple(component_paths["silero"])
        elif component == "whisperx" and "whisperx" in component_paths:
            paths = tuple(component_paths["whisperx"])
        elif component == "voxcpm" and "voxcpm" in component_paths:
            paths = tuple(component_paths["voxcpm"])
        elif component == "fishs2" and "fishs2" in component_paths:
            paths = tuple(component_paths["fishs2"])

        overrides = {module.config_flag: True} if module.config_flag else {}
        if component == "xtts":
            overrides["cuda_support"] = cuda_support
        elif component == "kokoro":
            overrides["kokoro_gpu_support"] = kokoro_gpu_support
        elif component == "kokoro_cpu":
            overrides["kokoro_gpu_support"] = False
        elif component == "xtts_finetuning":
            overrides["cuda_support"] = cuda_support
            overrides["xtts_support"] = True
            overrides["whisperx_support"] = True

        block_definitions[component] = BlockDefinition(
            name=component,
            source_root=source_root,
            include_paths=paths,
            required_markers=module.markers,
            config_overrides=overrides,
        )

    for block in block_definitions.values():
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
    for block_name in block_definitions:
        block = block_definitions[block_name]
        cached_block_paths[block_name] = ensure_block_cache(
            block,
            cache_root,
            force_refresh=args.force_refresh,
            prefer_hardlinks=prefer_hardlinks,
        )

    if args.name:
        archive_name = args.name
    else:
        if len(selected_modules) == 0:
            archive_name = "Pandrator-Core"
        else:
            components_display = "-".join(c.replace("_", "-").title() for c in selected_modules)
            archive_name = f"Pandrator-{components_display}"

    print(f"Assembling package: {archive_name}")
    package_stage_root = staging_root / archive_name
    package_install_root = package_stage_root / "Pandrator"

    if package_stage_root.exists():
        remove_tree(package_stage_root)

    package_install_root.mkdir(parents=True, exist_ok=True)
    hardlink_or_copy_file(
        installer_executable,
        package_stage_root / installer_executable.name,
        prefer_hardlinks,
    )

    for block_name in block_definitions:
        overlay_block(cached_block_paths[block_name], package_install_root, prefer_hardlinks)

    config_path = package_install_root / "config.json"
    apply_config_flags(config_path, config_flags, block_definitions)

    archive_path = output_root / f"{archive_name}.zip"
    create_zip_archive(package_stage_root, archive_path)
    archive_size = archive_path.stat().st_size

    print("Built archive:")
    print(f"- {archive_name}: {archive_path} ({format_size(archive_size)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
