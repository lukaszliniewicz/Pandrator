"""Application sessions domain request builders."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests


class ApplicationSessionMethods(ApplicationRequests):
    def list_sessions(
        self,
        *,
        limit: int = 50,
        query: str | None = None,
        include_trashed: bool = False,
    ) -> dict[str, Any]:
        parameters: dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
        if query and query.strip():
            parameters["q"] = query.strip()
        if include_trashed:
            parameters["include_trashed"] = "true"
        return self._request_json(
            "/api/v1/sessions",
            parameters=parameters,
        )

    def get_audiobook_setup(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/audiobook-setup")

    def configure_audiobook(
        self,
        session_id: str,
        *,
        expected_revision: str,
        mode: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/audiobook-setup",
            method="PATCH",
            body={"expected_revision": expected_revision, "mode": mode},
            idempotency_key=idempotency_key,
            maximum_body_bytes=4096,
        )

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}")

    def preview_session_deletion(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/purge-preview")

    def delete_session_permanently(
        self,
        session_id: str,
        *,
        expected_revision: int,
        impact_token: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/purge",
            method="POST",
            body={
                "expected_revision": expected_revision,
                "impact_token": impact_token,
            },
        )

    def get_session_trash_policy(self) -> dict[str, Any]:
        return self._request_json("/api/v1/session-trash-policy")

    def update_session_trash_policy(
        self,
        *,
        expected_revision: int,
        days: int | None,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/session-trash-policy",
            method="PATCH",
            body={"expected_revision": expected_revision, "days": days},
        )

    def get_workflow(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/workflow")

    def get_subtitles(self, session_id: str) -> dict[str, Any]:
        return self._request_json(f"/api/v1/sessions/{quote(session_id, safe='')}/subtitles")

    def fork_session(
        self,
        session_id: str,
        *,
        checkpoint_artifact_id: str,
        expected_revision: int,
        idempotency_key: str,
        name: str | None = None,
        carry_media_assets: bool = True,
        target_language: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "checkpoint_artifact_id": checkpoint_artifact_id,
            "expected_revision": expected_revision,
            "carry_media_assets": carry_media_assets,
        }
        if name is not None:
            body["name"] = name
        if target_language is not None:
            body["target_language"] = target_language
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/forks",
            method="POST",
            body=body,
            idempotency_key=idempotency_key,
        )

    def review_subtitles(
        self,
        session_id: str,
        *,
        artifact_ids: list[str] | tuple[str, ...],
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/subtitles/review",
            parameters=[("artifact_id", item) for item in artifact_ids],
        )

    def save_subtitle_review(
        self,
        session_id: str,
        stage: str,
        *,
        expected_revision: int,
        segments: list[dict[str, Any]],
        source_artifact_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "expected_revision": expected_revision,
            "segments": segments,
        }
        if source_artifact_id:
            payload["source_artifact_id"] = source_artifact_id
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/subtitles/{quote(stage, safe='')}/review",
            method="POST",
            body=payload,
            idempotency_key=idempotency_key,
        )

    def get_session_settings(
        self,
        session_id: str,
        section: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/{quote(section, safe='')}"
        )

    def describe_parameters(
        self,
        *,
        sections: tuple[str, ...] = (),
        names: tuple[str, ...] = (),
        workflow_kind: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return filtered parameter definitions from the application."""

        parameters: list[tuple[str, Any]] = [
            *(("section", section) for section in sections),
            *(("name", name) for name in names),
        ]
        if workflow_kind:
            parameters.append(("workflow_kind", workflow_kind))
        if query:
            parameters.append(("query", query))
        parameters.append(("limit", max(1, min(int(limit), 300))))
        return self._request_json(
            "/api/v1/parameter-definitions",
            parameters=parameters,
        )

    def create_session(
        self,
        *,
        name: str,
        workflow_kind: str,
        source_language: str,
        target_language: str | None,
        workflow_preset: str,
        included_stages: tuple[str, ...],
        idempotency_key: str,
        multilingual_setup: Any = None,
    ) -> dict[str, Any]:
        return self._request_json(
            "/api/v1/sessions",
            method="POST",
            body={
                "name": name,
                "workflow_kind": workflow_kind,
                "source_language": source_language,
                "target_language": target_language,
                "workflow_preset": workflow_preset,
                "included_stages": list(included_stages),
                **(
                    {
                        "multilingual_setup": multilingual_setup.model_dump(mode="json")
                        if hasattr(multilingual_setup, "model_dump")
                        else multilingual_setup
                    }
                    if multilingual_setup is not None
                    else {}
                ),
            },
            idempotency_key=idempotency_key,
        )

    def update_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
        changes: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}",
            method="PATCH",
            body=changes,
            idempotency_key=idempotency_key,
            if_match_revision=expected_revision,
        )

    def trash_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Move one session to recoverable trash with an If-Match guard."""

        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}",
            method="DELETE",
            if_match_revision=expected_revision,
        )

    def restore_session(
        self,
        session_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Restore one trashed session with an If-Match guard."""

        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/restore",
            method="POST",
            if_match_revision=expected_revision,
        )

    def update_session_settings(
        self,
        session_id: str,
        *,
        section: str,
        value: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/{quote(section, safe='')}",
            method="PUT",
            body={"value": value},
            idempotency_key=idempotency_key,
            if_match_revision=expected_revision,
        )

    def patch_session_settings(
        self,
        session_id: str,
        *,
        section: str,
        value: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Merge top-level fields into one stored session settings override."""

        return self._request_json(
            f"/api/v1/sessions/{quote(session_id, safe='')}/settings/{quote(section, safe='')}",
            method="PATCH",
            body={"value": value},
            idempotency_key=idempotency_key,
            if_match_revision=expected_revision,
        )
