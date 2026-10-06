"""Log metadata attached to native installer subprocesses.

Keep the native Popen identity and the historical metadata attributes: cleanup
must act on the same owned process even when polling or termination fails.
"""

from __future__ import annotations

import subprocess
from typing import TextIO

_LOG_HANDLE_ATTRIBUTE = "log_handle"
_LOG_PATH_ATTRIBUTE = "log_file_path"


def attach_process_log(
    process: subprocess.Popen[bytes], handle: TextIO, path: str | None = None
) -> None:
    setattr(process, _LOG_HANDLE_ATTRIBUTE, handle)
    if path is not None:
        setattr(process, _LOG_PATH_ATTRIBUTE, path)


def process_log_handle(process: object) -> TextIO | None:
    """Read the optional handle installed by attach_process_log."""
    return getattr(process, _LOG_HANDLE_ATTRIBUTE, None)


def clear_process_log_handle(process: object) -> None:
    setattr(process, _LOG_HANDLE_ATTRIBUTE, None)
