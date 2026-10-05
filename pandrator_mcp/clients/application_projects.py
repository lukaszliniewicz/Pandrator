"""Application projects domain request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationProjectMethods(ApplicationRequests):
    def get_translation_project(self, session_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/translation-project"
        )

    def create_translation_project(
        self,
        session_id: str,
        *,
        checkpoint_artifact_id: str,
        expected_revision: int,
        idempotency_key: str,
        name: str | None = None,
        create_planned_branches: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "checkpoint_artifact_id": checkpoint_artifact_id,
            "expected_revision": expected_revision,
            "create_planned_branches": create_planned_branches,
        }
        if name is not None:
            body["name"] = name
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/translation-project",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def create_translation_branches(
        self,
        project_id: str,
        *,
        expected_revision: int,
        targets: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-projects/{quote(project_id, safe='')}/branches",
            method="POST",
            body={"expected_revision": expected_revision, "targets": targets},
            idempotency_key=idempotency_key,
        )

    def preview_translation_project_operation(
        self,
        project_id: str,
        *,
        selected_branch_ids: list[str],
        expected_project_revision: int,
        action: str,
        export_kind: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-projects/{quote(project_id, safe='')}/operations/preview",
            method="POST",
            body={
                "selected_branch_ids": selected_branch_ids,
                "expected_project_revision": expected_project_revision,
                "action": action,
                "export_kind": export_kind,
            },
            idempotency_key=idempotency_key,
        )

    def get_translation_project_operation(self, operation_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-project-operations/{quote(operation_id, safe='')}"
        )

    def execute_translation_project_operation(
        self,
        operation_id: str,
        *,
        preview_digest: str,
        accepted_confirmations: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-project-operations/{quote(operation_id, safe='')}/execute",
            method="POST",
            body={
                "preview_digest": preview_digest,
                "accepted_confirmations": accepted_confirmations,
            },
            idempotency_key=idempotency_key,
        )

    def cancel_translation_project_operation(
        self,
        operation_id: str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-project-operations/{quote(operation_id, safe='')}/cancel",
            method="POST",
            body={},
            idempotency_key=idempotency_key,
        )

    def retry_translation_project_operation_preview(
        self,
        operation_id: str,
        *,
        expected_project_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-project-operations/{quote(operation_id, safe='')}/retry-preview",
            method="POST",
            body={"expected_project_revision": expected_project_revision},
            idempotency_key=idempotency_key,
        )

    def get_translation_project_export_manifest(self, operation_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-project-operations/{quote(operation_id, safe='')}/exports/manifest"
        )

    def request_translation_project_export_bundle(
        self,
        operation_id: str,
        *,
        expected_manifest_digest: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/translation-project-operations/{quote(operation_id, safe='')}/exports/bundle",
            method="POST",
            body={"expected_manifest_digest": expected_manifest_digest},
            idempotency_key=idempotency_key,
        )
