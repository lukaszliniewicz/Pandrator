"""Read-only, model-safe provider, artifact, and voice projections."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas import (
    DescribeParametersInput,
    ListArtifactsInput,
    ProviderStatusInput,
    VoiceCatalogInput,
)


def _safe_subtitle_diagnostics(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"status": "unavailable"}
    count_keys = (
        "cue_count", "line_length_violating_cues", "line_length_violating_lines",
        "reading_speed_violating_cues", "max_lines_violating_cues",
        "invalid_duration_cues", "observed_max_units_per_line", "observed_max_cps",
    )
    profile = value.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    limits = profile.get("limits")
    limits = limits if isinstance(limits, dict) else {}
    defaults = profile.get("language_defaults")
    defaults = defaults if isinstance(defaults, dict) else {}
    warning = profile.get("warning")
    warning = warning if isinstance(warning, dict) else {}
    def safe_causes(raw: object) -> list[dict[str, str]]:
        return [
            {key: str(cause[key]) for key in ("field", "origin") if isinstance(cause.get(key), str)}
            for cause in raw if isinstance(cause, dict)
        ] if isinstance(raw, list) else []
    safe_limits = {}
    for key in (
        "max_chars_per_line", "max_lines", "min_duration_ms", "max_duration_ms",
        "max_chars_per_second", "min_gap_ms", "phrase_gap_ms", "hard_gap_ms",
        "sentence_boundary_threshold",
    ):
        field = limits.get(key)
        if isinstance(field, dict):
            safe_limits[key] = {
                name: field[name] for name in ("effective", "supplied", "origin", "effective_origin")
                if name in field and isinstance(field[name], (str, int, float, bool, type(None)))
            }
    safe_profile = {
        "language": profile.get("language") if isinstance(profile.get("language"), str) else "",
        "language_origin": profile.get("language_origin") if isinstance(profile.get("language_origin"), str) else "unknown/historical_snapshot",
        "language_defaults": {
            "effective": defaults.get("effective") is True,
            "supplied": defaults.get("supplied") if isinstance(defaults.get("supplied"), bool) else None,
            "origin": str(defaults.get("origin") or "unknown/historical_snapshot"),
            "causes": safe_causes(defaults.get("causes")),
        },
        "limits": safe_limits,
        "warning": {
            "code": str(warning.get("code") or ""),
            "language": str(warning.get("language") or ""),
            "origin": str(warning.get("origin") or "unknown/historical_snapshot"),
            "causes": safe_causes(warning.get("causes")),
        } if warning else None,
    }
    return {
        "profile": safe_profile,
        **{
            phase: {
                key: counts[key] for key in count_keys
                if key in counts and isinstance(counts[key], (int, float))
            } | {"semantics": "display units; strict greater-than limits; millisecond cue duration"}
            for phase in ("input", "final")
            if isinstance(counts := value.get(phase), dict)
        },
    }


def describe_parameters(
    runtime: McpRuntime,
    arguments: DescribeParametersInput,
) -> dict[str, Any]:
    """Return the application's filtered, safe parameter-definition catalogue."""

    return runtime.require_application().describe_parameters(
        sections=arguments.sections,
        names=arguments.names,
        workflow_kind=arguments.workflow_kind,
        query=arguments.query,
        limit=arguments.limit,
    )


def list_artifacts(
    runtime: McpRuntime,
    arguments: ListArtifactsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_artifacts(
        session_id=arguments.session_id,
        limit=arguments.limit,
    )
    source = payload.get("items")
    items: list[dict[str, Any]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            if arguments.kind and item.get("kind") != arguments.kind:
                continue
            if arguments.role and item.get("role") != arguments.role:
                continue
            metadata = item.get("metadata_json")
            metadata = metadata if isinstance(metadata, dict) else {}
            diagnostic = metadata.get("subtitle_diagnostics")
            tracks = metadata.get("subtitle_tracks")
            items.append(
                {
                    "id": item.get("id"),
                    "session_id": item.get("session_id"),
                    "kind": item.get("kind"),
                    "role": item.get("role"),
                    "mime_type": item.get("mime_type"),
                    "size_bytes": item.get("size_bytes"),
                    "state": item.get("state"),
                    "created_at": item.get("created_at"),
                    "language": metadata.get("language") if isinstance(metadata.get("language"), str) else None,
                    "subtitle_diagnostics": _safe_subtitle_diagnostics(diagnostic),
                    "subtitle_tracks": [
                        {
                            "artifact_id": track.get("artifact_id"),
                            "language": track.get("language"),
                            "subtitle_diagnostics": _safe_subtitle_diagnostics(track.get("subtitle_diagnostics")),
                        }
                        for track in tracks if isinstance(track, dict)
                    ] if isinstance(tracks, list) else [],
                    **({"settings_hash": item["settings_hash"]} if item.get("settings_hash") else {}),
                }
            )
            if len(items) >= arguments.limit:
                break
    return {"schema_version": "1", "items": items}


def provider_status(
    runtime: McpRuntime,
    arguments: ProviderStatusInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_providers()
    source = payload.get("items")
    items: list[dict[str, Any]] = []
    if isinstance(source, list):
        for item in source:
            if not isinstance(item, dict):
                continue
            if not arguments.include_disabled and not item.get("enabled"):
                continue
            items.append(
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "provider_key": item.get("provider_key"),
                    "label": item.get("label"),
                    "enabled": bool(item.get("enabled")),
                    "base_url": item.get("base_url"),
                    "credential_backend": item.get("credential_backend"),
                    "credential_configured": bool(item.get("credential_configured")),
                    "revision": item.get("revision"),
                }
            )
    return {"schema_version": "1", "items": items}


def voice_catalog(
    runtime: McpRuntime,
    arguments: VoiceCatalogInput,
) -> dict[str, Any]:
    # Kept as the historical import path; the normalized catalog implementation
    # lives beside the lifecycle tools to keep the public tool contract in one
    # place.
    from .voice_lifecycle import voice_catalog as normalized_voice_catalog

    return normalized_voice_catalog(runtime, arguments)
