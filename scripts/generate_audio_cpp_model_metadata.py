"""Generate Manager metadata from the application's canonical model catalogue."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = REPOSITORY_ROOT / "pandrator_manager" / "audio_cpp_model_metadata.json"
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

# Keep project imports after the repository-root sys.path bootstrap above.
from pandrator.logic.audio_cpp_catalogue import (  # noqa: E402
    inventory,
    package_metadata,
)


def generated_projection() -> dict[str, Any]:
    source_inventory = inventory()
    model_ids = sorted(package["id"] for package in source_inventory["packages"])
    return {
        "schema_version": 1,
        "runtime_version": source_inventory["runtime_version"],
        "models": {model_id: package_metadata(model_id) for model_id in model_ids},
    }


def serialized_projection() -> str:
    return json.dumps(generated_projection(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the Manager projection is missing or out of date",
    )
    args = parser.parse_args(argv)
    expected = serialized_projection()
    if args.check:
        if not OUTPUT_PATH.is_file():
            print(f"Missing generated projection: {OUTPUT_PATH}", file=sys.stderr)
            return 1
        if OUTPUT_PATH.read_text(encoding="utf-8") != expected:
            print(
                "Generated audio.cpp model metadata is stale; run "
                "scripts/generate_audio_cpp_model_metadata.py.",
                file=sys.stderr,
            )
            return 1
        print("Generated audio.cpp model metadata is current.")
        return 0

    OUTPUT_PATH.write_text(expected, encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
