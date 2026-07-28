"""Temporary deprecated pandrator-installer command alias."""

from __future__ import annotations

import sys

from .cli import main as manager_main


def main(argv: list[str] | None = None) -> int:
    print(
        "pandrator-installer is deprecated; use pandrator-manager. "
        "The Qt installer is feature-frozen during the WebUI migration.",
        file=sys.stderr,
    )
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["open"]
    return manager_main(arguments)
