#!/usr/bin/env python3
"""Build the canonical signed Manager self-update manifest."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)
from packaging.version import Version


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sign Windows and Linux Pandrator Manager runtime bundles."
    )
    parser.add_argument("--key", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--sequence", type=int, required=True)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--windows-bundle", type=Path, required=True)
    parser.add_argument("--linux-bundle", type=Path, required=True)
    parser.add_argument(
        "--repository",
        default="lukaszliniewicz/Pandrator",
    )
    parser.add_argument(
        "--minimum-manager-version",
        default="0.9.0",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist") / "pandrator-manager-release.json",
    )
    return parser.parse_args()


def _load_key(path: Path) -> tuple[str, Ed25519PrivateKey]:
    selected = path.expanduser().resolve(strict=True)
    payload = json.loads(selected.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("algorithm") != "ed25519"
    ):
        raise RuntimeError("The release key file has an unsupported schema.")
    private = Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(str(payload["private_key"]), validate=True)
    )
    actual_public = base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode("ascii")
    if actual_public != payload.get("public_key"):
        raise RuntimeError("The release key public/private values do not match.")
    return str(payload["key_id"]), private


def _bundle(path: Path, *, product: str, version: str) -> Path:
    selected = path.expanduser().resolve(strict=True)
    if not selected.is_file() or selected.is_symlink() or selected.suffix != ".zip":
        raise RuntimeError(f"Manager runtime bundle is not a regular ZIP: {selected}")
    with zipfile.ZipFile(selected) as archive:
        try:
            metadata = json.loads(
                archive.read("pandrator-release.json").decode("utf-8")
            )
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Manager runtime bundle metadata is invalid: {selected}"
            ) from error
    if (
        metadata.get("product") != product
        or metadata.get("version") != version
        or metadata.get("runtime_kind") != "native_launcher"
    ):
        raise RuntimeError(
            f"Manager runtime bundle identity does not match {product} {version}: "
            f"{selected}"
        )
    return selected


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    Version(args.version)
    Version(args.minimum_manager_version)
    if args.sequence < 1:
        raise ValueError("--sequence must be positive.")
    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    from pandrator_manager.releases.models import ReleaseEnvelope, ReleasePayload
    from pandrator_manager.releases.trust import canonical_json

    key_id, private = _load_key(args.key)
    windows = _bundle(
        args.windows_bundle,
        product="pandrator-manager",
        version=args.version,
    )
    linux = _bundle(
        args.linux_bundle,
        product="pandrator-manager",
        version=args.version,
    )
    base_url = (
        f"https://github.com/{args.repository}/releases/download/"
        f"{quote(args.release_tag, safe='')}"
    )

    def artifact(path: Path, system: str) -> dict:
        return {
            "filename": path.name,
            "url": f"{base_url}/{quote(path.name)}",
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "kind": "zip",
            "systems": [system],
            "architectures": ["x86_64"],
            "python_tags": [],
        }

    signed = {
        "schema_version": 1,
        "product": "pandrator-manager",
        "channel": "stable",
        "version": args.version,
        "sequence": args.sequence,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "minimum_manager_version": args.minimum_manager_version,
        "artifacts": [
            artifact(windows, "Windows"),
            artifact(linux, "Linux"),
        ],
        "key_rotation": None,
    }
    normalized_signed = ReleasePayload.model_validate(signed).model_dump(
        mode="json"
    )
    envelope = {
        "signed": normalized_signed,
        "signatures": [
            {
                "key_id": key_id,
                "signature": base64.b64encode(
                    private.sign(canonical_json(normalized_signed))
                ).decode("ascii"),
            }
        ],
    }
    validated = ReleaseEnvelope.model_validate(envelope).model_dump(mode="json")
    output = args.output
    if not output.is_absolute():
        output = repo_root / output
    output = output.resolve(strict=False)
    _write_json(output, validated)
    digest = sha256_file(output)
    checksum = output.with_suffix(output.suffix + ".sha256")
    checksum.write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(
        json.dumps(
            {
                "artifact": str(output),
                "key_id": key_id,
                "sequence": args.sequence,
                "sha256": digest,
                "version": args.version,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
