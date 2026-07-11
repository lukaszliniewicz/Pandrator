"""Generate the deterministic OpenAPI source consumed by the Svelte client."""

from __future__ import annotations

import json
from pathlib import Path

from pandrator.web.openapi import build_openapi_document


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    destination = root / "openapi.json"
    destination.write_text(
        json.dumps(build_openapi_document(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
