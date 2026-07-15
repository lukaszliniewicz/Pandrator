#!/usr/bin/env python3
"""Build the release AppImage in the pinned Linux container environment."""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_DIR = REPO_ROOT / "containers" / "appimage"
DEFAULT_IMAGE_NAME = "pandrator-appimage-builder"
DEFAULT_PLATFORM = "linux/amd64"
# The release lock currently contains linux-64 packages only. The host may be
# Windows, macOS, x86_64 Linux, or ARM Linux with container emulation, but the
# produced release artifact remains x86_64 until linux-aarch64 is locked too.
SUPPORTED_PLATFORMS = ("linux/amd64",)


def display_command(command: Sequence[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def run(command: Sequence[str], *, cwd: Path | None = None) -> None:
    printable = [str(part) for part in command]
    print(f"+ {display_command(printable)}", flush=True)
    subprocess.run(printable, check=True, cwd=str(cwd) if cwd else None)


def resolve_runtime(requested: str) -> str:
    candidates = (requested,) if requested != "auto" else ("docker", "podman")
    failures: list[str] = []

    for candidate in candidates:
        executable = shutil.which(candidate)
        if not executable:
            failures.append(f"{candidate} is not installed")
            continue

        result = subprocess.run(
            [executable, "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            return executable
        failures.append(f"{candidate} is installed but its engine is unavailable")

    details = "; ".join(failures)
    raise RuntimeError(
        "No usable container runtime was found. Start Docker or Podman and retry"
        + (f" ({details})." if details else ".")
    )


def container_fingerprint(container_dir: Path = CONTAINER_DIR) -> str:
    digest = hashlib.sha256()
    for name in ("Dockerfile", "build.sh"):
        path = container_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def image_reference(image_name: str, container_dir: Path = CONTAINER_DIR) -> str:
    final_component = image_name.rsplit("/", 1)[-1]
    if ":" in final_component or "@" in image_name:
        raise RuntimeError("--image-name must not include a tag or digest")
    return f"{image_name}:{container_fingerprint(container_dir)}"


def image_exists(runtime: str, image: str) -> bool:
    result = subprocess.run(
        [runtime, "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def build_image_command(
    runtime: str,
    image: str,
    target_platform: str,
    container_dir: Path = CONTAINER_DIR,
    *,
    no_cache: bool = False,
) -> list[str]:
    command = [
        runtime,
        "build",
        "--platform",
        target_platform,
        "--file",
        str(container_dir / "Dockerfile"),
        "--tag",
        image,
    ]
    if no_cache:
        command.append("--no-cache")
    command.append(str(container_dir))
    return command


def host_user_spec() -> str | None:
    if platform.system() not in {"Linux", "Darwin"}:
        return None
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return None
    return f"{os.getuid()}:{os.getgid()}"


def container_run_command(
    runtime: str,
    image: str,
    target_platform: str,
    repo_root: Path,
    output_dir: Path,
    appimage_args: Sequence[str],
    *,
    user_spec: str | None,
) -> list[str]:
    command = [runtime, "run", "--rm", "--platform", target_platform]
    runtime_name = Path(runtime).name.lower()
    if runtime_name in {"podman", "podman.exe"}:
        # Rootless Podman on SELinux hosts otherwise requires relabeling the
        # checkout. Disabling labels for this disposable build container keeps
        # the source mount read-only without mutating host file labels.
        command.extend(["--security-opt", "label=disable"])
        if user_spec:
            # Rootless Podman must map the invoking user into the container;
            # --user alone maps that UID into a subordinate namespace instead.
            command.extend(["--userns", "keep-id"])
    elif user_spec:
        command.extend(["--user", user_spec])
    command.extend(
        [
            "--volume",
            f"{repo_root}:/source:ro",
            "--volume",
            f"{output_dir}:/output",
            image,
            *appimage_args,
        ]
    )
    return command


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build the Pandrator installer AppImage in a pinned Debian container. "
            "Docker and Podman are supported on Windows, macOS, and Linux."
        )
    )
    parser.add_argument(
        "--runtime",
        choices=("auto", "docker", "podman"),
        default="auto",
        help="Container runtime to use (default: auto-detect Docker, then Podman).",
    )
    parser.add_argument(
        "--platform",
        choices=SUPPORTED_PLATFORMS,
        default=DEFAULT_PLATFORM,
        help=f"Target container/AppImage platform (default: {DEFAULT_PLATFORM}).",
    )
    parser.add_argument(
        "--image-name",
        default=DEFAULT_IMAGE_NAME,
        help="Local builder image name. A content fingerprint tag is added automatically.",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Host directory that receives the AppImage (default: dist).",
    )
    parser.add_argument(
        "--rebuild-image",
        action="store_true",
        help="Rebuild the builder image even when the fingerprinted image already exists.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the container engine's image-build cache. Implies --rebuild-image.",
    )
    parser.add_argument(
        "--no-smoke-test",
        action="store_true",
        help="Skip final packaged AppImage checks.",
    )
    parser.add_argument(
        "--no-network-smoke-test",
        action="store_true",
        help="Skip only the packaged HTTPS check; keep local and GUI checks.",
    )
    return parser.parse_args(argv)


def resolve_output_dir(path_value: str, repo_root: Path = REPO_ROOT) -> Path:
    output_dir = Path(path_value).expanduser()
    if not output_dir.is_absolute():
        output_dir = repo_root / output_dir
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        runtime = resolve_runtime(args.runtime)
        image = image_reference(args.image_name)
        output_dir = resolve_output_dir(args.output_dir)

        if args.rebuild_image or args.no_cache or not image_exists(runtime, image):
            run(
                build_image_command(
                    runtime,
                    image,
                    args.platform,
                    no_cache=args.no_cache,
                ),
                cwd=REPO_ROOT,
            )
        else:
            print(f"Using existing builder image: {image}")

        appimage_args: list[str] = []
        if args.no_smoke_test:
            appimage_args.append("--no-smoke-test")
        if args.no_network_smoke_test:
            appimage_args.append("--no-network-smoke-test")

        run(
            container_run_command(
                runtime,
                image,
                args.platform,
                REPO_ROOT,
                output_dir,
                appimage_args,
                user_spec=host_user_spec(),
            ),
            cwd=REPO_ROOT,
        )
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"AppImage container build failed: {error}", file=sys.stderr)
        return error.returncode if isinstance(error, subprocess.CalledProcessError) else 2

    print(f"Container build completed: {output_dir / 'PandratorInstaller-x86_64.AppImage'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
