"""Signed release trust, selection, activation, and rollback."""

from .authority import (
    SUPPORTED_RELEASE_PRODUCTS,
    ReleaseAuthority,
    VerifiedRelease,
)
from .bundles import (
    BUNDLE_METADATA_NAME,
    ValidatedReleaseBundle,
    active_manager_bundle,
    active_release_bundle,
    release_cache_path,
    validate_release_bundle,
)
from .models import (
    KeyRotation,
    ReleaseArtifact,
    ReleaseBundleMetadata,
    ReleaseEnvelope,
    ReleasePayload,
    ReleaseSignature,
    TrustedReleaseKey,
)
from .planning import ReleasePlanner
from .slots import ReleaseActivationError, ReleaseSlotManager
from .trust import (
    EMBEDDED_RELEASE_KEYS,
    ReleaseTrustNotProvisioned,
    TrustStore,
    VerifiedReleaseManifest,
    canonical_json,
    verify_release_manifest,
)

__all__ = [
    "EMBEDDED_RELEASE_KEYS",
    "BUNDLE_METADATA_NAME",
    "KeyRotation",
    "ReleaseActivationError",
    "ReleaseArtifact",
    "ReleaseAuthority",
    "ReleaseBundleMetadata",
    "ReleaseEnvelope",
    "ReleasePayload",
    "ReleaseSignature",
    "ReleasePlanner",
    "ReleaseSlotManager",
    "ReleaseTrustNotProvisioned",
    "TrustStore",
    "TrustedReleaseKey",
    "SUPPORTED_RELEASE_PRODUCTS",
    "VerifiedRelease",
    "ValidatedReleaseBundle",
    "active_manager_bundle",
    "active_release_bundle",
    "release_cache_path",
    "VerifiedReleaseManifest",
    "canonical_json",
    "verify_release_manifest",
    "validate_release_bundle",
]
