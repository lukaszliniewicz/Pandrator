"""User-facing contracts for optional project links and reviewed corrections."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .schemas.public_links import PUBLIC_LINK_ACTION_INPUT_MODELS


@dataclass(frozen=True, slots=True)
class PublicLinkOperation:
    action: str
    model: type[BaseModel]
    title: str
    scope: str
    method: str
    description: str

    @property
    def name(self) -> str:
        return f"pandrator_{self.action}"


_DETAILS = (
    (
        "get_public_sharing_settings",
        "Public sharing and hosting settings",
        "app.admin",
        "GET",
        "Read the Administrator-only sharing opt-in and effective hosting configuration. Link sharing is disabled by default; network hosting is configured separately.",
    ),
    (
        "configure_public_sharing",
        "Configure optional public sharing",
        "app.admin",
        "PUT",
        "An Administrator can enable sharing for the existing exact application origin at its current settings revision. Internet origins require HTTPS. Disabling revokes existing links and visitor sessions; it preserves submitted corrections. This does not configure a listener, proxy or firewall.",
    ),
    (
        "list_project_links",
        "Inspect project access links",
        "app.read",
        "GET",
        "A project owner or workspace Manager can inspect issued links, expiry, access type and revocation. Stored secrets and complete tokenized URLs are never listed. A project viewer or link visitor cannot issue or inspect access grants.",
    ),
    (
        "create_project_link",
        "Create a project or translation-review link",
        "app.write",
        "POST",
        "Create one labelled, expiring bearer link with a stable request UUID. Manage-project access permits project editing, processing and exports using configured resources, including paid services. Review-translation access pins the currently selected translation and permits pending text corrections only. The URL secret is returned once; send it privately. An uncertain retry with the same request key returns metadata without recovering the secret.",
    ),
    (
        "revoke_project_link",
        "Revoke a project access link",
        "app.write",
        "POST",
        "Revoke one link at its current revision and invalidate its visitor credentials. Existing submitted corrections remain reviewable. Already admitted processing follows normal job authorization and cancellation rules.",
    ),
    (
        "list_link_submissions",
        "List submitted translation corrections",
        "app.write",
        "GET",
        "List bounded pending, accepted or rejected corrections for one authorized project. A reviewer label is unverified attribution. Listing does not publish changes or run a provider. Ordinary project review authority and app.read/app.write are required.",
    ),
    (
        "get_link_submission",
        "Compare submitted translation corrections",
        "app.write",
        "GET",
        "Inspect bounded before/after passage corrections and the pinned artifact/hash/revision. Page through changes before deciding. A source_unavailable result preserves submitted text but cannot be accepted. This read does not change project output.",
    ),
    (
        "review_link_submission",
        "Accept or reject translation corrections",
        "app.write",
        "POST",
        "Explicitly accept or reject a submission with its expected revision and a stable idempotency key. Acceptance requires the inspected source hash, document revision and selected artifact, creates one immutable reviewed translation and selects it for output. Intervening project changes cause a conflict and leave the submission pending. Rejection changes only its disposition. Link visitors cannot perform this action.",
    ),
)

PUBLIC_LINK_OPERATIONS = tuple(
    PublicLinkOperation(
        action, PUBLIC_LINK_ACTION_INPUT_MODELS[action], title, scope, method, description
    )
    for action, title, scope, method, description in _DETAILS
)
