#!/usr/bin/env python3
"""Create a retained Ed25519 release key outside the source tree."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

DEFAULT_KEY_ID = "pandrator-2026-01"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the offline Pandrator Manager release key."
    )
    parser.add_argument("--key-id", default=DEFAULT_KEY_ID)
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            Path.home()
            / ".config"
            / "pandrator"
            / "release-keys"
            / f"{DEFAULT_KEY_ID}.json"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.expanduser().resolve(strict=False)
    if output.exists():
        raise RuntimeError(f"Refusing to replace an existing release key: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        Encoding.Raw,
        PublicFormat.Raw,
    )
    payload = {
        "schema_version": 1,
        "key_id": str(args.key_id),
        "algorithm": "ed25519",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "private_key": base64.b64encode(private_bytes).decode("ascii"),
        "public_key": base64.b64encode(public_bytes).decode("ascii"),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
        if os.name != "nt":
            output.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)

    print(
        json.dumps(
            {
                "key_id": args.key_id,
                "output": str(output),
                "public_key": payload["public_key"],
                "public_key_sha256": hashlib.sha256(public_bytes).hexdigest(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
