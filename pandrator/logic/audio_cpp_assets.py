"""On-demand audio.cpp model assets shared by transcription-adjacent workflows.

Only the fixed allowlist below can ever be fetched, from pinned immutable
sources. Status checks are strictly read-only: nothing is downloaded,
installed, or hashed just because availability was queried. The first
explicitly chosen use downloads only the selected model into the workspace
cache, which is separate from Manager-installed TTS assets (existing Manager
files are detected read-only and reused through a verified hardlink/copy).

Verified against audio.cpp 0.8.1 (0xShug0/audio.cpp, docs pinned at v0.8.1).
GGUF pins come from the Hugging Face Hub metadata for audio-cpp/audio.cpp-gguf
(LFS oid == file SHA-256, corroborated by the in-repo inventory manifest).
DeepFilterNet2 weights are not published in that Hub repo; they ship in the
upstream git tree (assets/framework/audio_utilities/deepfilternet2), pinned
to an immutable commit with an independently verified file SHA-256 digest
that is enforced like every other allowlist row.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from urllib.request import Request, urlopen

from .cancellable_process import ProcessCancelled

logger = logging.getLogger(__name__)

AUDIO_CPP_VERSION = "0.8.1"
HF_REPO = "audio-cpp/audio.cpp-gguf"
HF_REVISION = "406756ee8e3b16e902ce40112986c1010775f888"
DFN2_GIT_BLOB = "d2a4fe4e5dbb1a51f9ec41162460ce82ca4667a6"
_RESERVED_DISK_BYTES = 64 * 1024 * 1024
_USER_AGENT = "Pandrator-AudioCpp-Assets"

ProgressCallback = Callable[[str, int, int], None]


class AudioAssetsError(RuntimeError):
    """A pinned model asset cannot safely be provided."""


def _hf_url(directory: str, filename: str) -> str:
    return (
        f"https://huggingface.co/{HF_REPO}/resolve/{HF_REVISION}/{directory}/{filename}"
    )


MODELS: dict[str, dict] = {
    "qwen3_asr_0_6b": {
        "id": "qwen3_asr_0_6b",
        "label": "Qwen3-ASR 0.6B Q8_0 GGUF",
        "family": "qwen3_asr",
        "cli_task": "asr",
        "cli_family": "qwen3_asr",
        "directory": "Qwen3-ASR-0.6B-GGUF",
        "filename": "qwen3-asr-0.6b-q8_0.gguf",
        "kind": "gguf",
        "repo": HF_REPO,
        "revision": HF_REVISION,
        "url": _hf_url("Qwen3-ASR-0.6B-GGUF", "qwen3-asr-0.6b-q8_0.gguf"),
        "size_bytes": 1151272416,
        "sha256": "6c44ec2fb4cee513892d7863c1fcc3ea6b699ffa4d899b0ef4ab19956d9544f7",
        "utility": None,
        "notes": "Transcription model for the ASR agent (16 kHz input).",
    },
    "qwen3_asr_1_7b": {
        "id": "qwen3_asr_1_7b",
        "label": "Qwen3-ASR 1.7B Q8_0 GGUF",
        "family": "qwen3_asr",
        "cli_task": "asr",
        "cli_family": "qwen3_asr",
        "directory": "Qwen3-ASR-1.7B-GGUF",
        "filename": "qwen3-asr-1.7b-q8_0.gguf",
        "kind": "gguf",
        "repo": HF_REPO,
        "revision": HF_REVISION,
        "url": _hf_url("Qwen3-ASR-1.7B-GGUF", "qwen3-asr-1.7b-q8_0.gguf"),
        "size_bytes": 2473010048,
        "sha256": "da4fc2ac7f24dee784d1684eb1f35836cdbf559519452ae11777670734c0a4f8",
        "utility": None,
        "notes": "Larger transcription model for the ASR agent (16 kHz input).",
    },
    "bs_roformer": {
        "id": "bs_roformer",
        "label": "BS-RoFormer ep368 Q8_0 GGUF",
        "family": "bs_roformer",
        "cli_task": "sep",
        "cli_family": "bs_roformer",
        "directory": "BS-RoFormer-ep368-GGUF",
        "filename": "bs-roformer-ep368-q8_0.gguf",
        "kind": "gguf",
        "repo": HF_REPO,
        "revision": HF_REVISION,
        "url": _hf_url("BS-RoFormer-ep368-GGUF", "bs-roformer-ep368-q8_0.gguf"),
        "size_bytes": 172532256,
        "sha256": "9a55a8cad369d00f6e0fb208bb0cd87e30e25430772b8491e20a4eace6423ad2",
        "utility": None,
        "notes": "Vocal separation of 44.1 kHz mixtures; writes vocals.wav.",
    },
    "mel_band_roformer": {
        "id": "mel_band_roformer",
        "label": "Mel-Band RoFormer Q8_0 GGUF",
        "family": "mel_band_roformer",
        "cli_task": "sep",
        "cli_family": "mel_band_roformer",
        "directory": "Mel-Band-RoFormer-GGUF",
        "filename": "mel-band-roformer-q8_0.gguf",
        "kind": "gguf",
        "repo": HF_REPO,
        "revision": HF_REVISION,
        "url": _hf_url("Mel-Band-RoFormer-GGUF", "mel-band-roformer-q8_0.gguf"),
        "size_bytes": 251748928,
        "sha256": "2dd898ceb0e3812c18d6125dcd60174d35d3da22c94add76b029fbb21fc238fd",
        "utility": None,
        "notes": "Vocal separation of 44.1 kHz mixtures; named stems output.",
    },
    "deepfilternet2": {
        "id": "deepfilternet2",
        "label": "DeepFilterNet2 denoise weights",
        "family": "builtin_audio_utils",
        "cli_task": "s2s",
        "cli_family": "builtin_audio_utils",
        "directory": "deepfilternet2",
        "filename": "deepfilternet2.safetensors",
        "kind": "safetensors",
        "repo": "0xShug0/audio.cpp",
        "revision": "f2b4937306daa25f5c78520f3c626ed31495a37a",
        "url": (
            "https://raw.githubusercontent.com/0xShug0/audio.cpp/"
            "f2b4937306daa25f5c78520f3c626ed31495a37a"
            "/assets/framework/audio_utilities/"
            "deepfilternet2/deepfilternet2.safetensors"
        ),
        "size_bytes": 9368328,
        # Independently verified file digest of the 9368328-byte weight at the
        # immutable commit above (verified against /tmp/pandrator-dfn2-verified.json
        # and its downloaded copy; enforced like every other allowlist row).
        "sha256": "544740171cd81e55dcc7c5d1d889ec79df5808a75eb840a669a681f0d34b0626",
        "git_blob": DFN2_GIT_BLOB,
        "utility": "deepfilternet2",
        "notes": "48 kHz denoise/enhance via --load-option utility=deepfilternet2.",
    },
}

_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_VERIFIED: set[tuple[str, int, int, int]] = set()


def check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Audio asset operation was canceled.")


def model_info(model_id: str) -> dict:
    try:
        return dict(MODELS[model_id])
    except KeyError:
        raise AudioAssetsError(
            f"Unknown audio model {model_id!r}; supported: {', '.join(sorted(MODELS))}."
        ) from None


def _thread_lock(model_id: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(model_id, threading.Lock())


@contextmanager
def _model_lock(path: Path, model_id: str, event: threading.Event | None):
    """Thread lock plus process-wide file lock so one download happens once."""
    lock = _thread_lock(model_id)
    while not lock.acquire(timeout=0.1):
        check_cancelled(event)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a+b") as handle:
            handle.seek(0)
            if not handle.read(1):
                handle.write(b"0")
                handle.flush()
            while True:
                check_cancelled(event)
                try:
                    handle.seek(0)
                    if sys.platform == "win32":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as error:
                    if error.errno not in {
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    }:
                        raise
                    (event or threading.Event()).wait(0.1)
            try:
                yield
            finally:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


def resolve_executable(settings: dict) -> Path:
    explicit = str(
        settings.get("audio_cpp_executable") or os.environ.get("AUDIO_CPP_CLI") or ""
    ).strip()
    if explicit:
        candidate: Path | None = Path(explicit).expanduser()
    else:
        candidate = None
        workspace = os.environ.get("PANDRATOR_WORKSPACE", "")
        if workspace:
            pointer = Path(workspace) / "Pandrator/services/audio_cpp/current.json"
            try:
                slot = Path(json.loads(pointer.read_text(encoding="utf-8"))["path"])
                candidate = slot / (
                    "audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli"
                )
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if candidate is None:
            found = shutil.which("audiocpp_cli")
            candidate = Path(found) if found else None
    if candidate is None or not candidate.is_file():
        raise AudioAssetsError(
            "Vocal processing requires audio.cpp's audiocpp_cli. Install/update "
            "Local speech models (audio.cpp), or set AUDIO_CPP_CLI to that executable."
        )
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        raise AudioAssetsError(
            "The installed audiocpp_cli is not executable. Repair/update the "
            "audio.cpp runtime or correct its executable permission."
        )
    return candidate.resolve()


def cache_root(settings: dict) -> Path:
    configured = str(settings.get("audio_cpp_cache_dir") or "").strip()
    if configured:
        return Path(configured).expanduser()
    workspace = os.environ.get("PANDRATOR_WORKSPACE", "")
    if workspace:
        return Path(workspace) / "Pandrator/cache/audio_cpp"
    return Path.home() / ".cache/pandrator/audio_cpp"


def cache_path(model_id: str, settings: dict) -> Path:
    spec = model_info(model_id)
    return cache_root(settings) / model_id / spec["filename"]


def _manager_models_dirs() -> list[Path]:
    workspace = os.environ.get("PANDRATOR_WORKSPACE", "")
    if not workspace:
        return []
    service = Path(workspace) / "Pandrator/services/audio_cpp"
    if not service.is_dir():
        return []
    roots = []
    slots = list(service.iterdir())
    versions = service / "versions"
    if versions.is_dir():
        slots.extend(versions.iterdir())
    for slot in sorted(slots):
        models = slot / "models"
        if slot.is_dir() and models.is_dir():
            roots.append(models)
    return roots


def find_manager_asset(model_id: str) -> Path | None:
    """Read-only probe for a Manager-installed file of matching size."""
    spec = model_info(model_id)
    for models_root in _manager_models_dirs():
        candidate = models_root / spec["directory"] / spec["filename"]
        try:
            if candidate.is_file() and candidate.stat().st_size == spec["size_bytes"]:
                return candidate
        except OSError:
            continue
    return None


def _cache_key(path: Path) -> tuple[str, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _hash_file(path: Path, event: threading.Event | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            check_cancelled(event)
            digest.update(chunk)
    return digest.hexdigest()


def _verified_cached(path: Path, spec: dict, event: threading.Event | None) -> bool:
    """Fast path: exact size, plus memoized or freshly computed digest."""
    try:
        if not path.is_file() or path.stat().st_size != spec["size_bytes"]:
            return False
    except OSError:
        return False
    if spec["sha256"] is None:
        return True
    key = _cache_key(path)
    if key is not None and key in _VERIFIED:
        return True
    try:
        if _hash_file(path, event) != spec["sha256"]:
            return False
    except OSError:
        return False
    if key is not None:
        _VERIFIED.add(key)
    return True


def _reuse_manager_asset(
    destination: Path,
    spec: dict,
    event: threading.Event | None,
    progress: ProgressCallback | None,
) -> bool:
    """Hardlink (fallback copy) a verified Manager file into the cache."""
    source = find_manager_asset(spec["id"])
    if source is None:
        return False
    check_cancelled(event)
    expected = spec["sha256"]
    if expected is not None and _hash_file(source, event) != expected:
        logger.warning("Ignoring Manager asset with unexpected digest: %s", source)
        return False
    if progress is not None:
        progress("reuse", 0, spec["size_bytes"])
    check_cancelled(event)
    try:
        os.link(source, destination)
    except OSError as error:
        # Cross-device or otherwise un-linkable: fall back to a private copy.
        logger.debug("Hardlink reuse failed (%s); copying instead.", error)
        try:
            shutil.copyfile(source, destination)
        except OSError as copy_error:
            raise AudioAssetsError(
                f"Could not reuse the Manager {spec['id']} asset: {copy_error}"
            ) from copy_error
    if progress is not None:
        progress("reuse", spec["size_bytes"], spec["size_bytes"])
    key = _cache_key(destination)
    if key is not None and expected is not None:
        _VERIFIED.add(key)
    return True


def ensure_model(
    model_id: str,
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
    *,
    opener: Callable = urlopen,
) -> Path:
    """Download (once) and return the verified local path for an allowlist model.

    Progress stages, forwarded from processing calls: "download" (bytes),
    "verify" (bytes), "reuse" (bytes, Manager hardlink/copy).
    """
    spec = model_info(model_id)
    check_cancelled(cancel_event)
    destination = cache_path(model_id, settings)
    with _model_lock(destination, model_id, cancel_event):
        if _verified_cached(destination, spec, cancel_event):
            return destination.resolve()
        if find_manager_asset(model_id) is not None:
            try:
                if _reuse_manager_asset(
                    destination, spec, cancel_event, progress
                ) and _verified_cached(destination, spec, cancel_event):
                    return destination.resolve()
            except AudioAssetsError:
                raise
            except OSError as error:
                raise AudioAssetsError(
                    f"Could not reuse the Manager {model_id} asset: {error}"
                ) from error
            destination.unlink(missing_ok=True)
        try:
            free = shutil.disk_usage(destination.parent).free
        except OSError as error:
            raise AudioAssetsError(
                f"Could not check free space for the {spec['label']} download: {error}"
            ) from error
        if free < spec["size_bytes"] + _RESERVED_DISK_BYTES:
            raise AudioAssetsError(
                f"Not enough free space for the {spec['label']} "
                f"({spec['size_bytes']} bytes)."
            )
        temporary: Path | None = None
        try:
            logger.info(
                "Downloading the pinned %s (%s bytes).",
                spec["label"],
                spec["size_bytes"],
            )
            request = Request(spec["url"], headers={"User-Agent": _USER_AGENT})
            with (
                opener(request, timeout=30) as response,
                tempfile.NamedTemporaryFile(
                    dir=destination.parent,
                    prefix=f".{model_id}-download-",
                    delete=False,
                ) as output,
            ):
                temporary = Path(output.name)
                digest = hashlib.sha256()
                size = 0
                if progress is not None:
                    progress("download", 0, spec["size_bytes"])
                while True:
                    check_cancelled(cancel_event)
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > spec["size_bytes"]:
                        raise AudioAssetsError(
                            f"{spec['label']} download exceeded its pinned size."
                        )
                    digest.update(chunk)
                    output.write(chunk)
                    if progress is not None:
                        progress("download", size, spec["size_bytes"])
                if size != spec["size_bytes"]:
                    raise AudioAssetsError(
                        f"{spec['label']} download failed its pinned size check "
                        f"(got {size}, expected {spec['size_bytes']})."
                    )
                if spec["sha256"] is not None and digest.hexdigest() != spec["sha256"]:
                    raise AudioAssetsError(
                        f"{spec['label']} download failed its pinned SHA-256 check."
                    )
                output.flush()
                os.fsync(output.fileno())
            check_cancelled(cancel_event)
            if progress is not None:
                progress("verify", spec["size_bytes"], spec["size_bytes"])
            os.replace(temporary, destination)
            temporary = None
            key = _cache_key(destination)
            if key is not None and spec["sha256"] is not None:
                _VERIFIED.add(key)
        except OSError as error:
            raise AudioAssetsError(
                f"Could not cache {spec['label']}: {error}"
            ) from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return destination.resolve()


def availability(settings: dict | None = None) -> dict:
    """Read-only availability metadata. Never downloads, installs, or hashes."""
    values = dict(settings or {})
    try:
        executable = resolve_executable(values)
        runtime_available, reason = True, ""
    except AudioAssetsError as problem:
        executable, runtime_available, reason = None, False, str(problem)
    root = cache_root(values)
    models: dict[str, dict] = {}
    for model_id, spec in MODELS.items():
        cached = False
        try:
            cached = (root / model_id / spec["filename"]).stat().st_size == spec[
                "size_bytes"
            ]
        except OSError:
            cached = False
        models[model_id] = {
            "label": spec["label"],
            "family": spec["family"],
            "download_bytes": spec["size_bytes"],
            "cached": cached,
            "manager_asset_detected": find_manager_asset(model_id) is not None,
            # Size match only; full digest proof happens inside ensure_model.
            "sha256_verified": False,
            "sha256_available": spec["sha256"] is not None,
            "download_on_demand": runtime_available and not cached,
            "source_url": spec["url"],
        }
    return {
        "runtime": {
            # Minimum runtime this code was verified against. Mere presence of
            # an executable does not prove its version; the observed version
            # stays unknown until a CLI invocation reports it.
            "minimum_version": AUDIO_CPP_VERSION,
            "observed_version": None,
            "version_verified": False,
            "executable": str(executable) if executable else None,
            "available": runtime_available,
            "reason": reason,
        },
        "models": models,
    }
