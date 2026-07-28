"""Manager-owned launch adapters for upstream services.

These files are written into a versioned component slot after cloning. They
keep upstream repositories untouched while giving the manager stable ports,
data roots, and cross-platform dependency bootstrap contracts.
"""

from __future__ import annotations

KOKORO_MANIFEST_NAME = ".pandrator-manager/pixi.toml"
KOKORO_RUNNER_NAME = "pandrator-manager-run.py"
VOXCPM_RUNNER_NAME = "pandrator-manager-run.py"


KOKORO_MANIFEST = """\
[workspace]
name = "pandrator-manager-kokoro"
channels = ["conda-forge"]
platforms = ["win-64", "linux-64", "osx-64", "osx-arm64"]

[dependencies]
python = "3.11.*"
pip = "*"
ffmpeg = "*"
"""


KOKORO_RUNNER = r'''#!/usr/bin/env python3
"""Install the selected Kokoro runtime in its private Pixi environment and run it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(
    os.environ.get("PANDRATOR_KOKORO_STATE_DIR")
    or ROOT / ".pandrator-manager"
).expanduser().resolve()
STATE_FILE = STATE_DIR / "kokoro-runtime.json"
PYPI_INDEX = "https://pypi.org/simple"
TORCH_INDEXES = {
    "cpu": "https://download.pytorch.org/whl/cpu",
    "cuda-x86_64": "https://download.pytorch.org/whl/cu126",
    "cuda-aarch64": "https://download.pytorch.org/whl/cu129",
    "rocm": "https://download.pytorch.org/whl/rocm6.4",
}


def _run(command: list[str]) -> None:
    print("+ " + " ".join(command), file=sys.stderr, flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _fingerprint(backend: str) -> str:
    digest = hashlib.sha256()
    digest.update((ROOT / "pyproject.toml").read_bytes())
    digest.update(backend.encode("ascii"))
    digest.update(platform.machine().casefold().encode("ascii", errors="ignore"))
    digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode("ascii"))
    return digest.hexdigest()


def _runtime_ready(fingerprint: str) -> bool:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if state.get("fingerprint") != fingerprint:
        return False
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import fastapi, kokoro, soundfile, torch, uvicorn",
        ],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _install(backend: str, fingerprint: str) -> None:
    machine = platform.machine().casefold()
    if backend == "cuda":
        extra = "gpu"
        index = TORCH_INDEXES[
            "cuda-aarch64" if machine in {"aarch64", "arm64"} else "cuda-x86_64"
        ]
    elif backend == "rocm":
        extra = "rocm"
        index = TORCH_INDEXES["rocm"]
    else:
        extra = "cpu"
        index = None if backend == "metal" else TORCH_INDEXES["cpu"]

    _run([sys.executable, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--editable",
        f".[{extra}]",
    ]
    if index:
        command.extend(["--index-url", index, "--extra-index-url", PYPI_INDEX])
    _run(command)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "backend": backend,
                "fingerprint": fingerprint,
                "python": platform.python_version(),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, STATE_FILE)


def _ensure_model(model_root: Path) -> None:
    version_dir = model_root / "v1_0"
    model = version_dir / "kokoro-v1_0.pth"
    config = version_dir / "config.json"
    if model.is_file() and model.stat().st_size and config.is_file():
        return
    version_dir.mkdir(parents=True, exist_ok=True)
    _run(
        [
            sys.executable,
            str(ROOT / "docker" / "scripts" / "download_model.py"),
            "--output",
            str(version_dir),
        ]
    )


def _configure_espeak() -> None:
    """Initialize the wheel-bundled eSpeak runtime before Kokoro imports it."""

    import ctypes.util

    import espeakng_loader
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    required_data = ("phontab", "phondata", "phonindex")

    bundled_library = Path(espeakng_loader.get_library_path()).resolve()
    bundled_data = Path(espeakng_loader.get_data_path()).resolve()
    system_library = ctypes.util.find_library("espeak-ng")
    system_data_candidates = (
        Path("/usr/share/espeak-ng-data"),
        Path("/usr/lib/x86_64-linux-gnu/espeak-ng-data"),
        Path("/usr/lib64/espeak-ng-data"),
        Path("/usr/local/share/espeak-ng-data"),
        Path("/opt/homebrew/share/espeak-ng-data"),
    )
    system_data = next(
        (
            candidate.resolve()
            for candidate in system_data_candidates
            if all((candidate / name).is_file() for name in required_data)
        ),
        None,
    )
    if system_library and system_data is not None:
        # A system installation takes precedence. Some Linux loaders reuse an
        # already-loaded eSpeak SONAME, whose compiled data path can disagree
        # with the otherwise self-contained Python wheel.
        library: str = system_library
        data = system_data
    else:
        library = str(bundled_library)
        data = bundled_data

    selected_library = Path(library)
    if selected_library.is_absolute() and not selected_library.is_file():
        raise RuntimeError(f"The selected eSpeak library is missing: {library}")
    missing = [name for name in required_data if not (data / name).is_file()]
    if missing:
        raise RuntimeError(
            "The selected eSpeak data is incomplete: " + ", ".join(missing)
        )

    # phonemizer-fork reads the *_LIBRARY and *_DATA_PATH spellings. Keep the
    # upstream container aliases too because Kokoro also documents them.
    if selected_library.is_absolute():
        os.environ["PHONEMIZER_ESPEAK_LIBRARY"] = library
    else:
        os.environ.pop("PHONEMIZER_ESPEAK_LIBRARY", None)
    os.environ["PHONEMIZER_ESPEAK_DATA_PATH"] = str(data)
    os.environ["PHONEMIZER_ESPEAK_PATH"] = str(selected_library.parent)
    os.environ["PHONEMIZER_ESPEAK_DATA"] = str(data)
    os.environ["ESPEAK_DATA_PATH"] = str(data)
    # Misaki calls these loader helpers again at import time. Preserve the
    # validated selection so it cannot overwrite a working system runtime
    # with a conflicting shared-library instance.
    espeakng_loader.get_library_path = lambda: library
    espeakng_loader.get_data_path = lambda: str(data)
    EspeakWrapper.set_library(library)
    EspeakWrapper.set_data_path(str(data))
    print(f"Using eSpeak library {library} with data {data}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("cpu", "cuda", "rocm", "metal"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8880)
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args()

    fingerprint = _fingerprint(args.backend)
    if not _runtime_ready(fingerprint):
        _install(args.backend, fingerprint)

    model_root = args.model_dir.expanduser().resolve()
    _ensure_model(model_root)
    os.environ["MODEL_DIR"] = str(model_root)
    os.environ["VOICES_DIR"] = str((ROOT / "api" / "src" / "voices" / "v1_0").resolve())
    os.environ["USE_GPU"] = "false" if args.backend == "cpu" else "true"
    os.environ["DEVICE_TYPE"] = {
        "cpu": "cpu",
        "cuda": "cuda",
        "rocm": "cuda",
        "metal": "mps",
    }[args.backend]
    os.environ.setdefault("PYTHONUTF8", "1")
    _configure_espeak()

    import uvicorn

    uvicorn.run(
        "api.src.main:app",
        host=args.host,
        port=args.port,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


VOXCPM_RUNNER = r'''#!/usr/bin/env python3
"""Adapt the upstream VoxCPM bootstrapper to manager-owned network settings."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import run as upstream


ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("cuda", "cpu"), required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8021)
    parser.add_argument("--pixi-path", type=Path, required=True)
    args = parser.parse_args()

    upstream.PIXI_PATH_OVERRIDE = args.pixi_path.expanduser().resolve()

    def start_server() -> None:
        pixi = upstream._find_pixi()
        command = [
            pixi,
            "run",
            "--manifest-path",
            str(ROOT / "pyproject.toml"),
            "python",
            "-m",
            "uvicorn",
            upstream.SERVER_MODULE,
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--no-access-log",
        ]
        result = subprocess.run(command, cwd=ROOT, check=False)
        raise SystemExit(result.returncode)

    upstream.start_server = start_server
    sys.argv = [
        str(ROOT / "run.py"),
        "--backend",
        args.backend,
        "--pixi-path",
        str(upstream.PIXI_PATH_OVERRIDE),
    ]
    upstream.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def generated_runtime_files(component_id: str) -> dict[str, str]:
    if component_id == "kokoro":
        return {
            KOKORO_MANIFEST_NAME: KOKORO_MANIFEST,
            KOKORO_RUNNER_NAME: KOKORO_RUNNER,
        }
    if component_id == "voxcpm":
        return {VOXCPM_RUNNER_NAME: VOXCPM_RUNNER}
    return {}
