"""Embedded trust root, threshold verification, rotation, and anti-downgrade."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import Version

from .. import __version__
from .models import ReleaseEnvelope, ReleasePayload

# Public halves of the retained project release keys. Private halves are kept
# outside the repository and are used only by the release-signing script. The
# 2026-01 key remains trusted for historical manifests; 2026-02 is the active
# signing key from Manager 0.9.15 onward.
# Runtime APIs never accept replacement public keys or caller-selected roots.
EMBEDDED_RELEASE_KEYS: Mapping[str, str] = {
    "pandrator-2026-01": "yWL/8kp9Ojz0axmk3M9umjQKXbBlOEvZ6ctbGBszPSs=",
    "pandrator-2026-02": "JYscD3JCYhfzJmrod0rC3x9BxNlK3Hr+4lOZmcJJhgU=",
}


class ReleaseTrustNotProvisioned(RuntimeError):
    """Raised when a production release trust root has not been embedded."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class VerifiedReleaseManifest:
    payload: ReleasePayload
    digest: str
    verified_key_ids: tuple[str, ...]
    raw_signed: dict[str, Any]

    def select_artifact(
        self,
        *,
        system: str,
        architecture: str,
        python_tag: str | None = None,
        python_tags: Collection[str] | None = None,
    ):
        system_key = system.casefold()
        architecture_key = architecture.casefold()
        system_aliases = {
            "windows": {"windows", "win32", "win"},
            "win32": {"windows", "win32", "win"},
            "linux": {"linux"},
        }.get(system_key, {system_key})
        architecture_aliases = {
            "amd64": {"amd64", "x86_64"},
            "x86_64": {"amd64", "x86_64"},
            "arm64": {"arm64", "aarch64"},
            "aarch64": {"arm64", "aarch64"},
        }.get(architecture_key, {architecture_key})
        supported_python_tags = {
            value.casefold()
            for value in (
                *((python_tag,) if python_tag is not None else ()),
                *(python_tags or ()),
            )
        }
        matches = [
            artifact
            for artifact in self.payload.artifacts
            if system_aliases.intersection(
                value.casefold() for value in artifact.systems
            )
            and architecture_aliases.intersection(
                value.casefold() for value in artifact.architectures
            )
            and (
                not artifact.python_tags
                or supported_python_tags.intersection(
                    value.casefold() for value in artifact.python_tags
                )
            )
        ]
        if len(matches) != 1:
            raise ValueError(
                "The signed release must select exactly one artifact for this "
                f"runtime; found {len(matches)}."
            )
        return matches[0]


class TrustStore:
    def __init__(
        self,
        keys: Mapping[str, str],
        *,
        threshold: int = 1,
        activation_sequence: int = 1,
    ) -> None:
        if not keys:
            raise ValueError("A release trust store cannot be empty.")
        if threshold < 1 or threshold > len(keys):
            raise ValueError("Release signature threshold is invalid.")
        parsed: dict[str, Ed25519PublicKey] = {}
        encoded: dict[str, str] = {}
        for key_id, value in keys.items():
            try:
                raw = base64.b64decode(value, validate=True)
                parsed[key_id] = Ed25519PublicKey.from_public_bytes(raw)
            except (ValueError, TypeError) as error:
                raise ValueError(
                    f"Release key {key_id!r} is not a raw Ed25519 public key."
                ) from error
            encoded[key_id] = value
        self.keys = parsed
        self.encoded_keys = encoded
        self.threshold = int(threshold)
        self.activation_sequence = int(activation_sequence)

    @classmethod
    def embedded(cls) -> "TrustStore":
        if not EMBEDDED_RELEASE_KEYS:
            raise ReleaseTrustNotProvisioned(
                "Pandrator release trust is not provisioned in this build. "
                "Signed application and manager updates are disabled."
            )
        return cls(EMBEDDED_RELEASE_KEYS)

    def verify(
        self,
        document: bytes | str | Mapping[str, Any],
        *,
        current_version: str | None = None,
        last_sequence: int = 0,
        current_manifest_digest: str | None = None,
        now: datetime | None = None,
        manager_version: str = __version__,
    ) -> VerifiedReleaseManifest:
        if isinstance(document, bytes):
            raw = json.loads(document.decode("utf-8"))
        elif isinstance(document, str):
            raw = json.loads(document)
        else:
            raw = dict(document)
        if not isinstance(raw.get("signed"), dict):
            raise ValueError("Release manifest signed payload is missing.")
        envelope = ReleaseEnvelope.model_validate(raw)
        signed = dict(raw["signed"])
        canonical = canonical_json(signed)
        digest = hashlib.sha256(canonical).hexdigest()
        verified: list[str] = []
        for signature in envelope.signatures:
            key = self.keys.get(signature.key_id)
            if key is None:
                continue
            try:
                decoded = base64.b64decode(signature.signature, validate=True)
                key.verify(decoded, canonical)
            except (ValueError, InvalidSignature):
                continue
            verified.append(signature.key_id)
        if len(set(verified)) < self.threshold:
            raise ValueError("Release manifest signature threshold was not met.")

        payload = envelope.signed
        selected_now = now or datetime.now(timezone.utc)
        published = payload.published_at
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published > selected_now + timedelta(hours=24):
            raise ValueError("Release manifest publication time is in the future.")
        if Version(manager_version) < Version(
            payload.minimum_manager_version or "0"
        ):
            raise ValueError(
                "This release requires a newer Pandrator Manager."
            )
        if payload.sequence < self.activation_sequence:
            raise ValueError("Release was signed by a key that is not active yet.")

        exact_replay = (
            current_manifest_digest is not None
            and current_manifest_digest == digest
        )
        if current_version is not None:
            candidate = Version(payload.version)
            installed = Version(current_version)
            if candidate < installed:
                raise ValueError("Release downgrade is not permitted.")
            if candidate == installed and not exact_replay:
                raise ValueError(
                    "A different manifest cannot replace the installed version."
                )
        if payload.sequence <= last_sequence and not exact_replay:
            raise ValueError("Release sequence is not newer than trusted state.")
        return VerifiedReleaseManifest(
            payload=payload,
            digest=digest,
            verified_key_ids=tuple(sorted(set(verified))),
            raw_signed=signed,
        )

    def rotated(self, manifest: VerifiedReleaseManifest) -> "TrustStore":
        rotation = manifest.payload.key_rotation
        if rotation is None:
            raise ValueError("Verified release does not authorize a key rotation.")
        return TrustStore(
            {key.key_id: key.public_key for key in rotation.keys},
            threshold=rotation.threshold,
            activation_sequence=rotation.activates_at_sequence,
        )


def verify_release_manifest(
    document: bytes | str | Mapping[str, Any],
    *,
    current_version: str | None = None,
    last_sequence: int = 0,
    current_manifest_digest: str | None = None,
) -> VerifiedReleaseManifest:
    """Verify only against the compiled-in product trust root."""

    return TrustStore.embedded().verify(
        document,
        current_version=current_version,
        last_sequence=last_sequence,
        current_manifest_digest=current_manifest_digest,
    )
