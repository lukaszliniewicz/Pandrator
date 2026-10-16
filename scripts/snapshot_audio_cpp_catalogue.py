#!/usr/bin/env python3
"""Build a deterministic, metadata-only catalogue for audio.cpp model specs.

The source catalogue is intentionally kept separate from the runtime model
manager.  This script reads only the root JSON files in a model_specs
directory, and uses the public Hugging Face API to resolve file metadata.  It
never downloads model weights or sends an authentication header.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, urlopen

RUNTIME_VERSION = "0.9.2"
SCHEMA_VERSION = 1
SOURCE_URL = "https://github.com/0xShug0/audio.cpp/tree/v0.9.2/model_specs"
GITHUB_DOCS_BASE = "https://github.com/0xShug0/audio.cpp/blob/v0.9.2/"
HF_BASE = "https://huggingface.co"
MAX_SMALL_FILE_BYTES = 16 * 1024 * 1024
LEGACY_SMALL_FILE_BYTES = 1 * 1024 * 1024
LARGE_SIDECAR_EXTENSIONS = frozenset({".json", ".txt", ".tiktoken", ".model", ".yaml", ".yml"})
NEVER_FETCH_EXTENSIONS = frozenset(
    {".gguf", ".safetensors", ".pt", ".pth", ".bin", ".npz", ".npy"}
)
MAX_WORKERS = 4
RETRY_COUNT = 2
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
GIT_BLOB_SHA1 = re.compile(r"^[0-9a-fA-F]{40}$")
COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")


class SnapshotError(RuntimeError):
    """A source or output error that is safe to show to callers."""


def _copy_json(value: Any) -> Any:
    """Copy JSON-compatible data without retaining source object references."""

    return copy.deepcopy(value)


def _safe_component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise SnapshotError(f"{label} must be a safe path component")
    if not SAFE_COMPONENT.fullmatch(value):
        raise SnapshotError(f"{label} must be a safe path component")
    return value


def safe_relative_path(value: Any, label: str) -> str:
    """Validate a POSIX relative path used by a package or remote file."""

    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or "//" in value
        or value.endswith("/")
        or any(marker in value for marker in "*?[]")
    ):
        raise SnapshotError(f"{label} must be a safe relative path")
    if value.startswith("/") or value.startswith("~"):
        raise SnapshotError(f"{label} must be a safe relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SnapshotError(f"{label} must be a safe relative path")
    return str(path)


def _safe_strip_prefix(value: Any, label: str) -> str:
    if value in (None, "", "."):
        return ""
    return safe_relative_path(value, label)


def _relative_package_path(source_path: str, strip_prefix: str) -> str:
    if not strip_prefix:
        return source_path
    prefix = strip_prefix.rstrip("/")
    if source_path == prefix or not source_path.startswith(prefix + "/"):
        raise SnapshotError(
            f"file path '{source_path}' does not start with strip_prefix '{strip_prefix}'"
        )
    relative = source_path[len(prefix) + 1 :]
    if not relative:
        raise SnapshotError(f"strip_prefix removes file path '{source_path}'")
    return safe_relative_path(relative, "package local file path")


def _safe_docs_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise SnapshotError("documentation links must be non-empty strings")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or not parsed.netloc:
            raise SnapshotError("documentation links must use HTTPS")
        return value
    relative = value[2:] if value.startswith("./") else value
    relative = safe_relative_path(relative, "documentation link")
    return urljoin(GITHUB_DOCS_BASE, relative)


def _documentation_links(spec: dict[str, Any]) -> list[str]:
    docs = (spec.get("ui") or {}).get("docs", [])
    if docs is None:
        return []
    if not isinstance(docs, list):
        raise SnapshotError(f"{spec.get('family', 'unknown')} docs must be a list")
    return [_safe_docs_url(item) for item in docs]


def load_specs(specs_dir: Path) -> list[dict[str, Any]]:
    """Load and validate only root ``*.json`` files from *specs_dir*."""

    if not specs_dir.is_dir():
        raise SnapshotError(f"specs directory does not exist: {specs_dir}")
    specs: list[dict[str, Any]] = []
    families: set[str] = set()
    for path in sorted(specs_dir.glob("*.json"), key=lambda item: item.name):
        family_from_filename = _safe_component(path.stem, "spec filename")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"could not read {path.name}: invalid JSON") from exc
        if not isinstance(value, dict):
            raise SnapshotError(f"{path.name} must contain a JSON object")
        family = _safe_component(value.get("family"), f"{path.name} family")
        if family != family_from_filename:
            raise SnapshotError(
                f"{path.name} family '{family}' does not match filename"
            )
        if family in families:
            raise SnapshotError(f"duplicate family id: {family}")
        families.add(family)
        value["_source_filename"] = path.name
        specs.append(value)
    return specs


def _merged_download(spec: dict[str, Any], package: dict[str, Any]) -> dict[str, Any]:
    defaults = spec.get("package_defaults") or {}
    if not isinstance(defaults, dict):
        raise SnapshotError(f"{spec['family']} package_defaults must be an object")
    default_download = defaults.get("download") or {}
    if not isinstance(default_download, dict):
        raise SnapshotError(f"{spec['family']} package_defaults.download must be an object")
    package_download = package.get("download") or {}
    if not isinstance(package_download, dict):
        raise SnapshotError(f"{package['id']} download must be an object")
    merged = dict(default_download)
    merged.update(package_download)
    return merged


def _runtime_metadata(package: dict[str, Any]) -> dict[str, Any] | None:
    """Retain package-level runtime metadata when a future spec supplies it."""

    runtime = package.get("runtime")
    if isinstance(runtime, dict):
        return _copy_json(runtime)
    useful_keys = ("task", "tasks", "load", "session")
    selected = {key: _copy_json(package[key]) for key in useful_keys if key in package}
    return selected or None


_COMPONENT_IDS = frozenset({
    "auk_qwen_bf16", "auk_qwen_q8_0", "auk_vae_f32", "vocos_mel_24khz",
    "yue2_vae_f16", "yue2_vae_f32",
})
_YUE2_MAIN_PACKAGE_IDS = frozenset(
    {"yue2_main_bf16", "yue2_main_q4_0", "yue2_main_q8_0"}
)
_YUE2_F16_VAE_SOURCE_PATH = "yue2-vae-f16.gguf"
_PACKAGE_TASK_OVERRIDES: dict[str, tuple[str, list[str]]] = {
    "personaplex_7b_v1_q4_k": ("conversion", ["s2s"]),
    "personaplex_7b_v1_q8_0": ("conversion", ["s2s"]),
    "dots_tts_edit_bf16": ("tools", ["edit"]),
    "dots_tts_edit_q8_0": ("tools", ["edit"]),
}
_SPEECH_TASK_OVERRIDES: dict[str, list[str]] = {
    "maya1_orig": ["tts", "design"],
    "maya1_q8_0": ["tts", "design"],
    "irodori_tts_600m_v3_voicedesign_f16": ["tts", "clone", "design"],
    "irodori_tts_600m_v3_voicedesign_q8_0": ["tts", "clone", "design"],
    "irodori_tts_v4_small_f16": ["tts", "clone", "design"],
    "irodori_tts_v4_small_q8_0": ["tts", "clone", "design"],
    "irodori_tts_500m_v3_f16": ["tts", "clone"],
    "irodori_tts_500m_v3_q8_0": ["tts", "clone"],
    "irodori_tts_v4_1_anime_q8_0": ["tts"],
    "moss_voicegen_bf16_codec_f16_decode": ["design"],
}


def _package_role_annotations(package_id: str) -> dict[str, Any]:
    if package_id in _COMPONENT_IDS:
        return {"entry_role": "component", "category": "component", "tasks": []}
    if package_id in _PACKAGE_TASK_OVERRIDES:
        category, tasks = _PACKAGE_TASK_OVERRIDES[package_id]
        return {"category": category, "tasks": tasks.copy()}
    if package_id in _SPEECH_TASK_OVERRIDES:
        return {"tasks": _SPEECH_TASK_OVERRIDES[package_id].copy()}
    return {}


def _flatten_family(spec: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": _safe_component(spec.get("family"), "family id"),
        "display_name": spec.get("display_name"),
        "description": spec.get("description"),
        "category": spec.get("category"),
        "status": spec.get("status"),
        "tasks": _copy_json(spec.get("tasks") or []),
        "modes": _copy_json(spec.get("modes") or []),
        "languages": _copy_json(spec.get("languages") or []),
        "capabilities": _copy_json(spec.get("capabilities") or {}),
        "options": _copy_json(spec.get("options") or {}),
    }
    docs = _documentation_links(spec)
    if docs:
        result["docs"] = docs
    return result


def flatten_packages(specs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten package defaults and validate all package/file paths."""

    packages: list[dict[str, Any]] = []
    package_ids: set[str] = set()
    for spec in specs:
        family = _safe_component(spec.get("family"), "family id")
        entries = spec.get("packages") or []
        if not isinstance(entries, list):
            raise SnapshotError(f"{family} packages must be a list")
        for package in entries:
            if not isinstance(package, dict):
                raise SnapshotError(f"{family} package must be an object")
            package_id = _safe_component(package.get("id"), "package id")
            if package_id in package_ids:
                raise SnapshotError(f"duplicate package id: {package_id}")
            package_ids.add(package_id)
            files = package.get("files") or []
            if not isinstance(files, list):
                raise SnapshotError(f"{package_id} files must be a list")
            source_files = [
                safe_relative_path(item, f"{package_id} file path") for item in files
            ]
            if (
                package_id in _YUE2_MAIN_PACKAGE_IDS
                and _YUE2_F16_VAE_SOURCE_PATH not in source_files
            ):
                source_files.append(_YUE2_F16_VAE_SOURCE_PATH)
            if len(source_files) != len(set(source_files)):
                raise SnapshotError(f"{package_id} contains duplicate file paths")
            strip_prefix = _safe_strip_prefix(
                package.get("strip_prefix"), f"{package_id} strip_prefix"
            )
            local_files = [
                _relative_package_path(item, strip_prefix) for item in source_files
            ]
            target_directory = safe_relative_path(
                package.get("target_directory"), f"{package_id} target_directory"
            )
            download = _merged_download(spec, package)
            kind = download.get("kind")
            if kind is not None and not isinstance(kind, str):
                raise SnapshotError(f"{package_id} download.kind must be a string")
            repo = download.get("repo")
            if repo is not None and (
                not isinstance(repo, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
            ):
                raise SnapshotError(f"{package_id} download.repo must be namespace/name")
            revision = download.get("revision")
            if revision is not None and (
                not isinstance(revision, str)
                or not revision
                or "\n" in revision
                or "\r" in revision
            ):
                raise SnapshotError(f"{package_id} download.revision is invalid")
            packages.append(
                {
                    "id": package_id,
                    "family": family,
                    **_package_role_annotations(package_id),
                    "label": package.get("display_name"),
                    "format": package.get("format"),
                    "precision": package.get("precision"),
                    "default": bool(package.get("default", False)),
                    "target_directory": target_directory,
                    "strip_prefix": strip_prefix,
                    "files": source_files,
                    "download": {
                        "kind": kind,
                        "repo": repo,
                        "revision": revision,
                        "gated": bool(download.get("gated", False)),
                    },
                    "_local_files": local_files,
                    "_runtime": _runtime_metadata(package),
                }
            )
    return sorted(packages, key=lambda item: (item["family"], item["id"]))


def _cache_key(url: str, suffix: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest() + suffix


def _sanitize_reason(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        return f"http_{error.code}"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, URLError):
        reason = str(error.reason).lower()
        if "timed out" in reason or "timeout" in reason:
            return "timeout"
        return "network_error"
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(error, SnapshotError):
        return str(error).replace("/", "_")[:120]
    return "network_error"


class _MetadataReader(Protocol):
    """Read-only metadata and bounded-content interface used by the snapshotter."""

    def get_json(self, url: str) -> dict[str, Any]: ...

    def get_small_file(self, url: str) -> bytes: ...


class MetadataClient:
    """Public, unauthenticated Hugging Face metadata client with a file cache."""

    def __init__(
        self,
        cache_dir: Path,
        opener: Callable[..., Any] = urlopen,
        retries: int = RETRY_COUNT,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.opener = opener
        self.retries = retries

    def _request(self, url: str, *, binary: bool, max_bytes: int | None = None) -> bytes:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "huggingface.co":
            raise SnapshotError("refusing non-public HTTPS metadata URL")
        suffix = ".bin" if binary else ".json"
        cache_path = self.cache_dir / _cache_key(url, suffix)
        try:
            with cache_path.open("rb") as cached_file:
                cached = cached_file.read(-1 if max_bytes is None else max_bytes + 1)
        except OSError:
            cached = None
        if cached is not None:
            if max_bytes is not None and len(cached) > max_bytes:
                raise SnapshotError("small metadata file exceeds size limit")
            return cached

        request = Request(
            url,
            headers={
                "Accept": "application/json" if not binary else "application/octet-stream",
                "User-Agent": "pandrator-audio-cpp-inventory/1",
            },
        )
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener(request, timeout=30) as response:
                    declared = response.headers.get("Content-Length")
                    if max_bytes is not None and declared and int(declared) > max_bytes:
                        raise SnapshotError("small metadata file exceeds size limit")
                    data = response.read(None if max_bytes is None else max_bytes + 1)
                if max_bytes is not None and len(data) > max_bytes:
                    raise SnapshotError("small metadata file exceeds size limit")
                temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
                temporary.write_bytes(data)
                os.replace(temporary, cache_path)
                return data
            except (HTTPError, URLError, TimeoutError, OSError, SnapshotError) as error:
                last_error = error
                if attempt < self.retries:
                    time.sleep(0.2 * (attempt + 1))
        assert last_error is not None
        raise SnapshotError(_sanitize_reason(last_error)) from last_error

    def get_json(self, url: str) -> dict[str, Any]:
        try:
            value = json.loads(self._request(url, binary=False))
        except json.JSONDecodeError as error:
            raise SnapshotError(_sanitize_reason(error)) from error
        if not isinstance(value, dict):
            raise SnapshotError("metadata_not_object")
        return value

    def get_small_file(self, url: str) -> bytes:
        return self._request(url, binary=True, max_bytes=MAX_SMALL_FILE_BYTES)


def _metadata_url(repo: str, revision: str) -> str:
    return (
        f"{HF_BASE}/api/models/{quote(repo, safe='/')}/revision/"
        f"{quote(revision, safe='')}?blobs=true"
    )


def _resolve_url(repo: str, revision: str, path: str) -> str:
    encoded_path = "/".join(quote(part, safe="") for part in path.split("/"))
    return f"{HF_BASE}/{quote(repo, safe='/')}/resolve/{quote(revision, safe='')}/{encoded_path}"


def _file_detail(
    source_path: str,
    strip_prefix: str,
    sibling: dict[str, Any] | None,
    client: _MetadataReader,
    repo: str,
    resolved_revision: str,
    allow_content: bool,
) -> tuple[dict[str, Any], bool]:
    relative_path = _relative_package_path(source_path, strip_prefix)
    detail: dict[str, Any] = {
        "source_path": source_path,
        "path": relative_path,
        "sha256": None,
        "size": None,
    }
    if sibling is None:
        return detail, False
    size = sibling.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        size = None
    lfs = sibling.get("lfs")
    if isinstance(lfs, dict):
        lfs_size = lfs.get("size")
        if isinstance(lfs_size, int) and not isinstance(lfs_size, bool) and lfs_size >= 0:
            size = lfs_size
        digest = lfs.get("sha256") or lfs.get("oid")
        if (
            isinstance(digest, str)
            and SHA256.fullmatch(digest)
            and size is not None
        ):
            detail["sha256"] = digest.lower()
            detail["size"] = size
            return detail, True
    detail["size"] = size
    extension = PurePosixPath(source_path).suffix.lower()
    within_legacy_limit = size is not None and size <= LEGACY_SMALL_FILE_BYTES
    is_large_sidecar = extension in LARGE_SIDECAR_EXTENSIONS
    may_fetch_content = (
        allow_content
        and extension not in NEVER_FETCH_EXTENSIONS
        and size is not None
        and size <= MAX_SMALL_FILE_BYTES
        and (within_legacy_limit or is_large_sidecar)
    )
    if may_fetch_content:
        try:
            content = client.get_small_file(_resolve_url(repo, resolved_revision, source_path))
        except SnapshotError:
            return detail, False
        if len(content) != size:
            return detail, False
        blob_id = sibling.get("blobId")
        if isinstance(blob_id, str) and GIT_BLOB_SHA1.fullmatch(blob_id):
            git_blob = hashlib.sha1(
                b"blob " + str(size).encode("ascii") + b"\0" + content
            ).hexdigest()
            if git_blob != blob_id.lower():
                return detail, False
        detail["sha256"] = hashlib.sha256(content).hexdigest()
        detail["size"] = len(content)
        return detail, True
    return detail, False


def _empty_availability(package: dict[str, Any]) -> dict[str, Any]:
    download = package["download"]
    kind = download.get("kind")
    repo = download.get("repo")
    unsupported = kind != "huggingface_snapshot" or not repo
    gated = bool(download.get("gated", False))
    return {
        "verified": False,
        "missing_files": [],
        "unverified_files": [],
        "gated": gated,
        "unsupported_download": unsupported,
        "unavailable_metadata": None,
    }


def build_snapshot(
    specs_dir: Path,
    cache_dir: Path | None = None,
    client: _MetadataReader | None = None,
) -> dict[str, Any]:
    """Build the complete catalogue, resolving public manifests concurrently."""

    specs = load_specs(specs_dir)
    families = [_flatten_family(spec) for spec in specs]
    packages = flatten_packages(specs)
    client = client or MetadataClient(cache_dir or Path("/tmp/audio-cpp-inventory-cache"))
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for package in packages:
        download = package["download"]
        if download["kind"] == "huggingface_snapshot" and download["repo"]:
            requested = download["revision"] or "main"
            grouped.setdefault((download["repo"], requested), []).append(package)

    metadata_keys = {
        key
        for key, entries in grouped.items()
        if any(not package["download"]["gated"] for package in entries)
    }
    metadata_cache: dict[tuple[str, str], dict[str, Any] | None] = {}
    metadata_errors: dict[tuple[str, str], str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(client.get_json, _metadata_url(repo, revision)): (repo, revision)
            for repo, revision in metadata_keys
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                metadata_cache[key] = future.result()
            except SnapshotError as error:
                metadata_cache[key] = None
                metadata_errors[key] = str(error)

    output_packages: list[dict[str, Any]] = []
    for package in packages:
        public = {
            key: _copy_json(value)
            for key, value in package.items()
            if not key.startswith("_")
        }
        runtime = package.get("_runtime")
        if runtime is not None:
            public["runtime"] = runtime
        availability = _empty_availability(package)
        if (
            package["download"]["kind"] == "huggingface_snapshot"
            and package["download"]["repo"]
            and not package["download"]["gated"]
        ):
            download = package["download"]
            key = (download["repo"], download["revision"] or "main")
            metadata = metadata_cache.get(key)
            if metadata is None:
                if not package["download"]["gated"]:
                    availability["unavailable_metadata"] = metadata_errors.get(
                        key, "metadata_unavailable"
                    )
            else:
                resolved_revision = metadata.get("sha")
                siblings = metadata.get("siblings")
                if not isinstance(resolved_revision, str) or not COMMIT_SHA.fullmatch(resolved_revision):
                    availability["unavailable_metadata"] = "metadata_missing_revision"
                elif not isinstance(siblings, list):
                    availability["unavailable_metadata"] = "metadata_missing_files"
                else:
                    sibling_map = {
                        item.get("rfilename"): item
                        for item in siblings
                        if isinstance(item, dict) and isinstance(item.get("rfilename"), str)
                    }
                    gated = bool(download.get("gated", False) or metadata.get("gated", False))
                    details: list[dict[str, Any]] = []
                    missing: list[str] = []
                    unverified: list[str] = []
                    for source_path in package["files"]:
                        sibling = sibling_map.get(source_path)
                        detail, ok = _file_detail(
                            source_path,
                            package["strip_prefix"],
                            sibling,
                            client,
                            download["repo"],
                            resolved_revision,
                            allow_content=not gated,
                        )
                        details.append(detail)
                        if sibling is None:
                            missing.append(source_path)
                        elif not ok:
                            unverified.append(source_path)
                    manifest: dict[str, Any] = {
                        "repository": download["repo"],
                        "requested_revision": download["revision"],
                        "revision": resolved_revision,
                        "files": details,
                    }
                    card_data = metadata.get("cardData")
                    if isinstance(card_data, dict) and isinstance(card_data.get("license"), str):
                        manifest["repository_license"] = {
                            "value": card_data["license"],
                            "url": f"{HF_BASE}/{quote(download['repo'], safe='/')}",
                            "note": "Repository card metadata only; individual model and component licensing remains unreviewed.",
                        }
                    public["weight_manifest"] = manifest
                    availability.update(
                        {
                            "verified": (
                                not gated
                                and not missing
                                and not unverified
                                and len(details) == len(package["files"])
                                and all(
                                    isinstance(item["size"], int)
                                    and not isinstance(item["size"], bool)
                                    and isinstance(item["sha256"], str)
                                    and SHA256.fullmatch(item["sha256"])
                                    for item in details
                                )
                            ),
                            "missing_files": missing,
                            "unverified_files": unverified,
                            "gated": gated,
                            "unavailable_metadata": None,
                        }
                    )
        public["availability"] = availability
        output_packages.append(public)

    return {
        "schema_version": SCHEMA_VERSION,
        "runtime_version": RUNTIME_VERSION,
        "source_url": SOURCE_URL,
        "families": sorted(families, key=lambda item: item["id"]),
        "packages": output_packages,
    }


def _write_json(path: Path, value: dict[str, Any], data: bytes | None = None) -> bytes:
    if data is None:
        data = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--specs-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mirror", type=Path)
    parser.add_argument("--cache-dir", type=Path, default=Path("/tmp/audio-cpp-inventory-cache"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        snapshot = build_snapshot(args.specs_dir, args.cache_dir)
        data = _write_json(args.output, snapshot)
        if args.mirror:
            _write_json(args.mirror, snapshot, data)
    except SnapshotError as error:
        print(f"snapshot failed: {error}", file=sys.stderr)
        return 2
    packages = snapshot["packages"]
    verified = sum(1 for package in packages if package["availability"]["verified"])
    speech = sum(
        1
        for family in snapshot["families"]
        if family["category"] in {"asr", "tts", "speech_analysis", "voice_conversion"}
    )
    print(
        f"wrote {len(snapshot['families'])} families / {len(packages)} packages "
        f"({verified} verified, {speech} speech families) to {args.output} "
        f"({len(data)} bytes)"
    )
    if args.mirror:
        print(f"wrote identical mirror to {args.mirror}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
