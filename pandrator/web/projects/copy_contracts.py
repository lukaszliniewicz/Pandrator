"""Immutable current-state input for a later, separately authorized project copy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from .copy_outputs import SAVED_SPEECH_ROLE

PROJECT_COPY_JOB_KIND = "project.copy.current"
COPY_STATE_VERSION = 15
SUPPORTED_COPY_STATE_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, COPY_STATE_VERSION})

_MESSAGES = {
    "copy_connection_unavailable": "A selected destination model is unavailable. Choose an enabled model in that workspace.",
    "copy_source_unavailable": "Restore the source project before copying it.",
    "copy_limit_exceeded": "This project exceeds the current copy limits.",
    "copy_graph_invalid": "The saved project state is incomplete or inconsistent.",
    "copy_file_unavailable": "A source file is unavailable or has changed. Refresh the copy preview.",
    "copy_staging_failed": "Project files could not be prepared for copying.",
    "copy_preview_stale": "The project changed. Refresh the copy preview before copying.",
    "copy_not_ready": "This project's saved state needs attention before it can be copied.",
    "copy_input_invalid": "The saved copy request is unavailable or inconsistent.",
    "copy_canceled": "Project copying was canceled.",
    "copy_publication_failed": "The project copy could not be completed.",
}


class ProjectCopyError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409):
        if code not in _MESSAGES or message != _MESSAGES[code]:
            raise ValueError("A fixed project copy error is required.")
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def project_copy_error(code: str) -> ProjectCopyError:
    return ProjectCopyError(code, _MESSAGES[code])


def public_copy_job_result(job_id: str) -> dict[str, str]:
    """Generic jobs/events expose no destination or frozen editorial inputs."""
    return {"copy_id": job_id}


class CopySettingsReader(Protocol):
    def get_in_session(self, session: Session, session_id: str, section: str) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CopyFileSource:
    artifact_id: str
    workspace_id: str
    project_id: str | None
    session_id: str | None
    relative_path: str
    content_hash: str | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class ProjectCopySnapshot:
    source_project_id: str
    destination_workspace_id: str
    name: str
    state_json: str
    fingerprint: str
    files: tuple[CopyFileSource, ...]
    blockers: tuple[tuple[str, str], ...]
    active_job_count: int
    destination_connection_options_json: str = "[]"

    @property
    def ready(self) -> bool:
        return not self.blockers

    def manifest(self) -> dict[str, Any]:
        state = json.loads(self.state_json)
        sessions = {item["id"]: item["name"] for item in state["rows"]["sessions"]}
        requirements = [
            {
                "id": item["id"],
                "purpose": item["purpose"],
                "session_count": len({target["session_id"] for target in item["targets"]}),
                "session_names": sorted(
                    {sessions[target["session_id"]] for target in item["targets"]}
                ),
                "selected_provider_id": item["provider_id"],
                "selected_provider_model_id": item["provider_model_id"],
            }
            for item in state.get("destination_connections", [])
        ]
        counts = {key: len(value) for key, value in state["rows"].items()}
        controls = sum(row["kind"] == "generation_controls" for row in state["rows"].get("knowledge_ledgers", []))
        if controls:
            counts["generation_controls"] = controls
        if state["schema_version"] >= 10:
            saved_outputs = sum(row["role"] == SAVED_SPEECH_ROLE for row in state["rows"].get("artifacts", []))
            if saved_outputs:
                counts["saved_speech_outputs"] = saved_outputs
        if state["schema_version"] >= 14:
            placement = sum(row["placement_status"] == "needs_placement"
                for row in state["rows"].get("project_sound_cues", []))
            if placement:
                counts["sound_needs_placement"] = placement
        return {
            "connection_requirements": requirements,
            "connection_options": json.loads(self.destination_connection_options_json),
            "source_project_id": self.source_project_id,
            "destination_workspace_id": self.destination_workspace_id,
            "name": self.name,
            "counts": counts,
            "file_count": len(self.files),
            "known_bytes": sum(
                item.size_bytes for item in self.files if item.size_bytes is not None
            ),
            "unknown_size_count": sum(item.size_bytes is None for item in self.files),
            "active_job_count": self.active_job_count,
            "ready": self.ready,
            "blockers": [{"code": code, "domain": domain} for code, domain in self.blockers],
        }
