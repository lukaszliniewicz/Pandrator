"""Run basedpyright against the interpreter selected by Pixi or the caller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    # Basedpyright can auto-select a neighbouring .venv even when invoked from
    # Pixi. Pin its import resolution to the interpreter running this command.
    return subprocess.call(
        [sys.executable, "-m", "basedpyright", "--pythonpath", sys.executable, *sys.argv[1:]],
        cwd=Path(__file__).resolve().parents[1],
    )


if __name__ == "__main__":
    raise SystemExit(main())
