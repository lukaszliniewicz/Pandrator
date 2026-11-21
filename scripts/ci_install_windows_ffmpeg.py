"""Install the FFmpeg build the Windows installer pins, for a CI job.

Chocolatey serves whatever FFmpeg is newest, so the Windows jobs ran against
a build that no managed install uses, and its feed fails often enough to fail
jobs. This downloads the archive the installer downloads, checks the same
SHA-256, unpacks it under the runner's temporary directory and adds its
``bin`` directory to the PATH of the job's later steps.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pandrator_installer.constants import (  # noqa: E402
    FFMPEG_SUBTITLES_WINDOWS_ZIP_SHA256,
    FFMPEG_SUBTITLES_WINDOWS_ZIP_URL,
)

ATTEMPTS = 4


def download(url: str, target: Path) -> None:
    """Fetch ``url`` to ``target``, waiting longer after each failure."""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(url, timeout=60) as response, target.open("wb") as out:  # noqa: S310
                shutil.copyfileobj(response, out)
        except OSError as error:
            if attempt == ATTEMPTS:
                raise
            delay = 10 * attempt
            print(f"Download failed ({error}); trying again in {delay} s.", flush=True)
            time.sleep(delay)
        else:
            return


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    root = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "pandrator-ffmpeg"
    root.mkdir(parents=True, exist_ok=True)
    archive = root / "ffmpeg.zip"
    download(FFMPEG_SUBTITLES_WINDOWS_ZIP_URL, archive)
    digest = file_sha256(archive)
    if digest != FFMPEG_SUBTITLES_WINDOWS_ZIP_SHA256:
        print(
            f"The FFmpeg archive has SHA-256 {digest}, "
            f"not the pinned {FFMPEG_SUBTITLES_WINDOWS_ZIP_SHA256}.",
            file=sys.stderr,
        )
        return 1
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(root)
    ffmpeg = next(root.glob("*/bin/ffmpeg.exe"), None)
    if ffmpeg is None or not (ffmpeg.parent / "ffprobe.exe").is_file():
        print("The FFmpeg archive holds no bin/ffmpeg.exe with bin/ffprobe.exe.", file=sys.stderr)
        return 1
    for tool in ("ffmpeg.exe", "ffprobe.exe"):
        subprocess.run([str(ffmpeg.parent / tool), "-version"], check=True)
    github_path = os.environ.get("GITHUB_PATH")
    if github_path:
        with Path(github_path).open("a", encoding="utf-8") as handle:
            handle.write(f"{ffmpeg.parent}\n")
    print(f"FFmpeg is installed in {ffmpeg.parent}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
