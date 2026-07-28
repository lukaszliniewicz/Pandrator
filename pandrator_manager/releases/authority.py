"""Persisted release trust resolution and exact host artifact selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from packaging.tags import sys_tags

from ..context import ManagerContext
from ..errors import ManagerError
from ..state import ManagerStore
from .models import ReleaseArtifact, ReleaseEnvelope
from .trust import (
    ReleaseTrustNotProvisioned,
    TrustStore,
    VerifiedReleaseManifest,
    canonical_json,
)

SUPPORTED_RELEASE_PRODUCTS = frozenset({"pandrator", "pandrator-manager"})


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    manifest: VerifiedReleaseManifest
    artifact: ReleaseArtifact
    envelope: dict[str, Any]
    exact_replay: bool


class ReleaseAuthority:
    """Resolve trust without ever accepting keys from an API request."""

    def __init__(
        self,
        context: ManagerContext,
        store: ManagerStore,
        *,
        trust_root: TrustStore | None = None,
    ) -> None:
        self.context = context
        self.store = store
        self._injected_trust_root = trust_root

    def _root(self) -> TrustStore:
        if self._injected_trust_root is not None:
            return self._injected_trust_root
        try:
            return TrustStore.embedded()
        except ReleaseTrustNotProvisioned as error:
            raise ManagerError(
                "release_trust_not_provisioned",
                str(error),
                http_status=503,
            ) from error

    @staticmethod
    def _json_document(document: Mapping[str, Any]) -> dict[str, Any]:
        try:
            # A round trip both detaches mutable request state and rejects
            # non-JSON objects before signature verification.
            encoded = json.dumps(
                dict(document),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            decoded = json.loads(encoded)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ManagerError(
                "invalid_release_manifest",
                "Release manifest must be a finite JSON object.",
                http_status=400,
            ) from error
        if not isinstance(decoded, dict):
            raise ManagerError(
                "invalid_release_manifest",
                "Release manifest must be a JSON object.",
                http_status=400,
            )
        return decoded

    @staticmethod
    def _peek(document: dict[str, Any]) -> ReleaseEnvelope:
        try:
            envelope = ReleaseEnvelope.model_validate(document)
        except Exception as error:
            raise ManagerError(
                "invalid_release_manifest",
                "Release manifest does not match the supported signed schema.",
                {"reason": str(error)},
                400,
            ) from error
        if envelope.signed.product not in SUPPORTED_RELEASE_PRODUCTS:
            raise ManagerError(
                "unsupported_release_product",
                f"Unsupported release product: {envelope.signed.product}",
                {"product": envelope.signed.product},
                400,
            )
        return envelope

    @staticmethod
    def _apply_pending(
        trust: TrustStore,
        pending: list[tuple[int, TrustStore]],
        sequence: int,
    ) -> tuple[TrustStore, list[tuple[int, TrustStore]]]:
        remaining: list[tuple[int, TrustStore]] = []
        selected = trust
        for activation, replacement in sorted(pending, key=lambda item: item[0]):
            if activation <= sequence:
                selected = replacement
            else:
                remaining.append((activation, replacement))
        return selected, remaining

    def _trust_for(self, product: str, sequence: int) -> TrustStore:
        trust = self._root()
        pending: list[tuple[int, TrustStore]] = []
        last_authorized_activation = trust.activation_sequence
        for record in self.store.accepted_releases(product):
            record_sequence = int(record["sequence"])
            if record_sequence >= sequence:
                break
            trust, pending = self._apply_pending(
                trust,
                pending,
                record_sequence,
            )
            try:
                verified = trust.verify(
                    record["envelope"],
                    now=datetime.now(timezone.utc),
                )
            except Exception as error:
                raise ManagerError(
                    "release_trust_state_invalid",
                    "Persisted release trust history could not be verified.",
                    {
                        "product": product,
                        "sequence": record_sequence,
                        "reason": str(error),
                    },
                    500,
                ) from error
            if (
                verified.digest != record["manifest_digest"]
                or verified.payload.product != product
                or verified.payload.sequence != record_sequence
            ):
                raise ManagerError(
                    "release_trust_state_invalid",
                    "Persisted release trust history does not match its signed data.",
                    {"product": product, "sequence": record_sequence},
                    500,
                )
            rotation = verified.payload.key_rotation
            if rotation is not None:
                if rotation.activates_at_sequence <= last_authorized_activation:
                    raise ManagerError(
                        "release_trust_state_invalid",
                        "Persisted key-rotation boundaries are not strictly increasing.",
                        {
                            "product": product,
                            "sequence": record_sequence,
                        },
                        500,
                    )
                replacement = trust.rotated(verified)
                pending.append(
                    (rotation.activates_at_sequence, replacement)
                )
                last_authorized_activation = rotation.activates_at_sequence
        trust, _ = self._apply_pending(trust, pending, sequence)
        return trust

    @staticmethod
    def _python_tags() -> tuple[str, ...]:
        values: set[str] = {"py3"}
        for tag in sys_tags():
            values.add(str(tag))
            values.add(tag.interpreter)
        return tuple(sorted(values))

    def verify(
        self,
        document: Mapping[str, Any],
        *,
        expected_product: str | None = None,
    ) -> VerifiedRelease:
        raw = self._json_document(document)
        envelope = self._peek(raw)
        product = envelope.signed.product
        if expected_product is not None and product != expected_product:
            raise ManagerError(
                "release_product_mismatch",
                "The signed manifest is for a different product.",
                {
                    "expected_product": expected_product,
                    "actual_product": product,
                },
                400,
            )
        current = self.store.accepted_release(product)
        trust = self._trust_for(product, envelope.signed.sequence)
        try:
            verified = trust.verify(
                raw,
                current_version=(
                    str(current["version"]) if current is not None else None
                ),
                last_sequence=(
                    int(current["sequence"]) if current is not None else 0
                ),
                current_manifest_digest=(
                    str(current["manifest_digest"])
                    if current is not None
                    else None
                ),
            )
            artifact = verified.select_artifact(
                system=self.context.system,
                architecture=self.context.architecture,
                python_tags=self._python_tags(),
            )
        except ManagerError:
            raise
        except Exception as error:
            raise ManagerError(
                "release_verification_failed",
                "The signed release could not be accepted.",
                {"reason": str(error)},
                409,
            ) from error
        exact_replay = bool(
            current is not None
            and current["manifest_digest"] == verified.digest
        )
        return VerifiedRelease(
            manifest=verified,
            artifact=artifact,
            envelope=raw,
            exact_replay=exact_replay,
        )

    @staticmethod
    def signed_payload_digest(document: Mapping[str, Any]) -> str:
        raw = ReleaseAuthority._json_document(document)
        ReleaseAuthority._peek(raw)
        return hashlib.sha256(
            canonical_json(raw["signed"])
        ).hexdigest()
