"""Immutable, project-scoped bundles of already completed language exports."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import unicodedata
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select

from . import models as m
from .auth import Principal
from .multilingual_setup import canonical_language
from .project_operations import ProjectOperationError, TranslationProjectOperationService
from .settings_policy import stable_hash

ProgressCallback = Callable[[float, str | None], None]
_HASH = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE_FILENAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SAFE_EXTENSION = re.compile(r"^[a-z0-9]{1,10}$")
_ALLOWED_EXTENSIONS = frozenset(
    {
        "ass",
        "csv",
        "docx",
        "epub",
        "flac",
        "html",
        "jpeg",
        "jpg",
        "json",
        "m4a",
        "mkv",
        "mov",
        "mp3",
        "mp4",
        "md",
        "ogg",
        "opus",
        "pdf",
        "png",
        "srt",
        "txt",
        "vtt",
        "wav",
        "webm",
        "webp",
        "xml",
        "zip",
    }
)
_CHUNK_SIZE = 1024 * 1024


class ProjectExportBundleError(RuntimeError):
    """A safe, API-mappable rejection for project export bundles."""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 409,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class ProjectExportBundleCanceled(RuntimeError):
    """Raised when a worker observes cancellation during bundle creation."""


def _check_canceled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ProjectExportBundleCanceled("Project export bundle creation was canceled.")


def _slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = folded.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:40].rstrip("-")
    return slug or "project"


def _safe_extension(path: Path) -> str:
    suffix = path.suffix.casefold().removeprefix(".")
    if suffix in _ALLOWED_EXTENSIONS and _SAFE_EXTENSION.fullmatch(suffix):
        return suffix
    return "bin"


def _manifest_digest(document: dict[str, Any]) -> str:
    content = {key: value for key, value in document.items() if key != "manifest_digest"}
    encoded = json.dumps(
        content,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _content_url(artifact_id: str) -> str:
    return f"/api/v1/artifacts/{artifact_id}/content"


class ProjectExportBundleService:
    """Build verifiable manifests and durable bundles from export receipts only."""

    JOB_KIND = "project.exports.bundle"
    IDEMPOTENCY_OPERATION = "requestProjectExportBundle"
    MAX_CHILDREN = 20

    def __init__(self, services: Any):
        self.services = services
        self.database = services.database
        self.operations = TranslationProjectOperationService(services)

    def _managed_regular_file(self, relative_path: str) -> Path:
        raw = str(relative_path or "")
        candidate = PurePosixPath(raw)
        if (
            not raw
            or "\\" in raw
            or candidate.is_absolute()
            or ".." in candidate.parts
            or not candidate.parts
        ):
            raise ValueError("The export artifact path is unsafe.")

        lexical = self.services.paths.root.joinpath(*candidate.parts)
        current = self.services.paths.root
        if current.is_symlink():
            raise ValueError("The export artifact path is unsafe.")
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("The export artifact path is unsafe.")

        managed = self.services.paths.managed_path(raw)
        if managed != lexical or not managed.is_file():
            raise ValueError("The export artifact file is unavailable.")
        return managed

    def _open_managed_regular_file(self, relative_path: str):
        path = self._managed_regular_file(relative_path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("The export artifact must be a regular file.")
            return path, os.fdopen(descriptor, "rb")
        except Exception:
            os.close(descriptor)
            raise

    def _hash_file(
        self,
        relative_path: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> tuple[Path, int, str]:
        path, handle = self._open_managed_regular_file(relative_path)
        digest = hashlib.sha256()
        size = 0
        with handle:
            while chunk := handle.read(_CHUNK_SIZE):
                _check_canceled(cancel_event)
                digest.update(chunk)
                size += len(chunk)
        return path, size, digest.hexdigest()

    @staticmethod
    def _retained_nodes(child: dict[str, Any]) -> Iterator[dict[str, Any]]:
        seen: set[int] = set()
        current: dict[str, Any] | None = child
        while isinstance(current, dict) and id(current) not in seen:
            seen.add(id(current))
            if current.get("session_deleted"):
                break
            yield current
            nested = current.get("retained")
            current = nested if isinstance(nested, dict) else None

    @classmethod
    def _child_result(cls, child: dict[str, Any]) -> dict[str, Any]:
        for node in cls._retained_nodes(child):
            result = node.get("result")
            if not isinstance(result, dict):
                continue
            if (
                any(
                    isinstance(item, dict) and item.get("artifact_id")
                    for item in result.get("artifacts", [])
                )
                if isinstance(result.get("artifacts"), list)
                else False
            ):
                return result
            if any(result.get(key) for key in ("artifact_ids", "artifact_id")):
                return result
        return {}

    @classmethod
    def _child_artifact_ids(cls, child: dict[str, Any], db=None) -> list[str]:
        result = cls._child_result(child)
        values: list[str] = []
        artifacts = result.get("artifacts")
        for item in artifacts if isinstance(artifacts, list) else []:
            if isinstance(item, dict) and item.get("artifact_id"):
                values.append(str(item["artifact_id"]))
        if not values:
            raw_ids = result.get("artifact_ids")
            if isinstance(raw_ids, list):
                values.extend(str(value) for value in raw_ids if value)
        if not values and result.get("artifact_id"):
            values.append(str(result["artifact_id"]))
        if not values and db is not None:
            job_id = cls._child_job_id(child)
            job = db.get(m.Job, str(job_id or "")) if job_id else None
            job_result = job.result_json if job and isinstance(job.result_json, dict) else {}
            raw_ids = job_result.get("artifact_ids")
            if isinstance(raw_ids, list):
                values.extend(str(value) for value in raw_ids if value)
            if not values and job_result.get("artifact_id"):
                values.append(str(job_result["artifact_id"]))
        return list(dict.fromkeys(values))

    @staticmethod
    def _child_job_id(child: dict[str, Any]) -> str:
        for node in ProjectExportBundleService._retained_nodes(child):
            job_id = node.get("job_id")
            if job_id:
                return str(job_id)
        return ""

    def _public_reason(self, value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        redacted = str(self.services.redactor.redact(value)).strip()
        # Never copy local absolute paths into a public manifest or archive.
        redacted = re.sub(
            r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/)(?:[^\s,;]+[\\/]?)+",
            "[path redacted]",
            redacted,
        )
        return redacted[:500] or None

    def _target_matches(
        self, operation: m.TranslationProjectOperation, target: dict[str, Any]
    ) -> bool:
        captured = (operation.preview_json or {}).get("target") or {}
        return all(
            captured.get(key) == target.get(key)
            for key in ("instance_id", "canonical_origin", "application_version")
        )

    def _collect(
        self,
        operation_id: str,
        *,
        principal: Principal,
        target_identity: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            operation_view = self.operations.get(
                operation_id,
                principal=principal,
                target_identity=target_identity,
            )
        except ProjectOperationError:
            raise
        except KeyError as error:
            raise ProjectExportBundleError(
                "not_found", "Project operation not found.", 404
            ) from error

        if operation_view.get("action") != "export":
            raise ProjectExportBundleError(
                "export_operation_required",
                "A completed export operation is required to create this manifest.",
            )

        children_view = operation_view.get("children")
        if not isinstance(children_view, list) or not 1 <= len(children_view) <= self.MAX_CHILDREN:
            raise ProjectExportBundleError(
                "invalid_export_selection",
                "The project operation does not contain a valid selected-language set.",
                422,
            )

        with self.database.snapshot_session() as db:
            operation = db.get(m.TranslationProjectOperation, operation_id)
            if (
                operation is None
                or operation.principal_subject != principal.subject
                or operation.target_instance_id != str(target_identity.get("instance_id") or "")
            ):
                raise ProjectExportBundleError("not_found", "Project operation not found.", 404)
            if not self._target_matches(operation, target_identity):
                raise ProjectExportBundleError(
                    "target_identity_mismatch",
                    "The operation belongs to another application target.",
                )
            if operation.action != "export":
                raise ProjectExportBundleError(
                    "export_operation_required",
                    "A completed export operation is required to create this manifest.",
                )

            project = db.get(m.TranslationProject, operation.project_id)
            if project is None:
                raise ProjectExportBundleError(
                    "project_not_found", "Translation project not found.", 404
                )
            captured_revision = int((operation.preview_json or {}).get("project_revision") or 0)
            stored_children = operation.children_json or []
            stored_branch_ids = [str(row.get("branch_id") or "") for row in stored_children]
            view_by_branch = {
                str(row.get("branch_id") or ""): row
                for row in children_view
                if isinstance(row, dict)
            }
            if (
                not captured_revision
                or len(stored_branch_ids) != len(children_view)
                or len(set(stored_branch_ids)) != len(stored_branch_ids)
                or set(stored_branch_ids) != set(view_by_branch)
            ):
                raise ProjectExportBundleError(
                    "operation_selection_changed",
                    "The selected language branches no longer match the operation.",
                )

            checkpoint = db.get(m.Artifact, project.checkpoint_artifact_id)
            checkpoint_valid = bool(
                checkpoint
                and checkpoint.session_id == project.source_session_id
                and checkpoint.content_hash == project.source_content_hash
            )
            top_reasons: list[str] = []
            if not checkpoint_valid:
                top_reasons.append("The pinned source checkpoint record is unavailable or changed.")
            if project.revision != captured_revision:
                top_reasons.append(
                    "The project revision changed after this export operation was captured."
                )

            project_name = str(project.name)
            public_project_name = self.services.redactor.redact_value(project_name)
            if not isinstance(public_project_name, str):
                public_project_name = project_name
            slug = _slug(public_project_name)
            used_names: set[str] = {"manifest.json"}
            private_children: list[dict[str, Any]] = []
            public_children: list[dict[str, Any]] = []
            ordered_views = sorted(
                children_view,
                key=lambda row: (
                    str(row.get("target_language") or ""),
                    str(row.get("branch_id") or ""),
                ),
            )
            for child in ordered_views:
                _check_canceled(cancel_event)
                branch_id = str(child.get("branch_id") or "")
                branch = db.get(m.TranslationProjectBranch, branch_id)
                language = ""
                child_reasons: list[str] = []
                validation_blocked = False
                if branch is None or branch.project_id != project.id:
                    validation_blocked = True
                    child_reasons.append("The selected branch is no longer a project member.")
                else:
                    branch_checkpoint = db.get(m.Artifact, branch.source_checkpoint_artifact_id)
                    if (
                        branch.source_content_hash != project.source_content_hash
                        or branch_checkpoint is None
                        or branch_checkpoint.session_id != branch.session_id
                        or branch_checkpoint.content_hash != project.source_content_hash
                    ):
                        validation_blocked = True
                        child_reasons.append(
                            "The branch no longer matches the project's pinned source checkpoint."
                        )
                    try:
                        language = canonical_language(branch.target_language)
                    except ValueError:
                        validation_blocked = True
                        child_reasons.append("The selected branch has an invalid language tag.")
                if not language:
                    try:
                        language = canonical_language(str(child.get("target_language") or ""))
                    except ValueError:
                        language = ""
                session_record = db.get(m.SessionRecord, branch.session_id) if branch else None
                if branch and (session_record is None or session_record.trashed_at is not None):
                    validation_blocked = True
                    child_reasons.append("The selected language session is unavailable or trashed.")

                operation_state = str(child.get("state") or "unknown")
                state = operation_state
                child_reason = self._public_reason(child.get("reason"))
                if child_reason:
                    child_reasons.append(child_reason)
                if child.get("error"):
                    error = child.get("error")
                    error_message = error.get("message") if isinstance(error, dict) else error
                    safe_error = self._public_reason(error_message)
                    if safe_error:
                        child_reasons.append(safe_error)

                artifact_records: list[dict[str, Any]] = []
                child_files: list[dict[str, Any]] = []
                ids = self._child_artifact_ids(child, db)
                if operation_state in {"completed", "existing"} and not ids:
                    validation_blocked = True
                    child_reasons.append("The completed export has no artifact receipt.")
                if ids and branch and session_record and session_record.trashed_at is None:
                    for artifact_id in sorted(ids):
                        artifact = db.get(m.Artifact, artifact_id)
                        if (
                            artifact is None
                            or artifact.session_id != branch.session_id
                            or artifact.state != "current"
                            or not str(artifact.role or "").startswith("export")
                            or not artifact.content_hash
                            or not _HASH.fullmatch(str(artifact.content_hash).casefold())
                        ):
                            validation_blocked = True
                            child_reasons.append(
                                "An export receipt is missing, stale, or belongs to another session."
                            )
                            continue
                        try:
                            path, size, digest = self._hash_file(
                                artifact.relative_path, cancel_event=cancel_event
                            )
                        except (OSError, ValueError):
                            validation_blocked = True
                            child_reasons.append(
                                "An export artifact file is missing, unsafe, or unavailable."
                            )
                            continue
                        if digest != str(artifact.content_hash).casefold() or (
                            artifact.size_bytes is not None and size != artifact.size_bytes
                        ):
                            validation_blocked = True
                            child_reasons.append(
                                "An export artifact file no longer matches its recorded hash or size."
                            )
                            continue

                        extension = _safe_extension(path)
                        base_name = (
                            f"{slug}-{language}-r{captured_revision}-{digest[:12]}.{extension}"
                        )
                        filename = base_name
                        collision = 2
                        while filename in used_names:
                            filename = (
                                f"{slug}-{language}-r{captured_revision}-{digest[:12]}-"
                                f"{collision}.{extension}"
                            )
                            collision += 1
                        if not _LANGUAGE_FILENAME.fullmatch(language):
                            child_reasons.append(
                                "The language cannot be represented as a safe filename."
                            )
                            continue
                        used_names.add(filename)
                        output_kind = str(artifact.kind or "artifact")
                        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", output_kind):
                            output_kind = "artifact"
                        record = {
                            "artifact_id": artifact.id,
                            "sha256": digest,
                            "size_bytes": size,
                            "output_kind": output_kind,
                            "filename": filename,
                        }
                        artifact_records.append(record)
                        child_files.append(
                            {
                                **record,
                                "language": language,
                                "relative_path": artifact.relative_path,
                                "session_id": branch.session_id,
                                "role": artifact.role,
                            }
                        )

                if validation_blocked:
                    state = "blocked"
                complete_child = (
                    operation_state in {"completed", "existing"}
                    and bool(ids)
                    and len(artifact_records) == len(ids)
                    and not validation_blocked
                )
                if not complete_child and not child_reasons:
                    child_reasons.append("This selected language export is not complete.")
                reason = "; ".join(dict.fromkeys(child_reasons)) or None
                result_row: dict[str, Any] = {
                    "branch_id": branch_id,
                    "language": language or str(child.get("target_language") or ""),
                    "state": state,
                    "operation_state": operation_state,
                    "reason": reason,
                    "artifacts": artifact_records,
                }
                if artifact_records:
                    result_row.update(artifact_records[0])
                public_children.append(result_row)
                private_children.append(
                    {
                        "branch_id": branch_id,
                        "session_id": branch.session_id if branch else "",
                        "language": language or str(child.get("target_language") or ""),
                        "operation_state": operation_state,
                        "job_id": self._child_job_id(child),
                        "files": child_files,
                    }
                )

            if top_reasons:
                for row in public_children:
                    row["state"] = "blocked"
                    existing = [row["reason"]] if row.get("reason") else []
                    row["reason"] = "; ".join(dict.fromkeys([*existing, *top_reasons]))

            complete = bool(
                checkpoint_valid
                and project.revision == captured_revision
                and public_children
                and all(
                    row["operation_state"] in {"completed", "existing"}
                    and row["state"] != "blocked"
                    and bool(row["artifacts"])
                    for row in public_children
                )
            )
            document: dict[str, Any] = {
                "manifest_version": 1,
                "project_id": project.id,
                "project_name": public_project_name,
                "project_revision": captured_revision,
                "source_checkpoint": {
                    "artifact_id": project.checkpoint_artifact_id,
                    "sha256": project.source_content_hash,
                },
                "operation_id": operation.id,
                "export_kind": str(operation_view.get("export_kind") or "configured"),
                "complete": complete,
                "languages": public_children,
            }
            if project.revision != captured_revision:
                document["current_project_revision"] = project.revision
            if top_reasons:
                document["blocked_reasons"] = top_reasons
            safe_document = self.services.redactor.redact_value(document)
            safe_document["manifest_digest"] = _manifest_digest(safe_document)
            private = {
                "operation_id": operation.id,
                "project_id": project.id,
                "project_name_fingerprint": stable_hash(project_name),
                "project_revision": captured_revision,
                "current_project_revision": project.revision,
                "export_kind": str(operation_view.get("export_kind") or "configured"),
                "principal_subject": operation.principal_subject,
                "target_identity": {
                    key: target_identity.get(key)
                    for key in ("instance_id", "canonical_origin", "application_version")
                },
                "source_session_id": project.source_session_id,
                "source_checkpoint": {
                    "artifact_id": project.checkpoint_artifact_id,
                    "sha256": project.source_content_hash,
                },
                "manifest": safe_document,
                "manifest_digest": safe_document["manifest_digest"],
                "languages": private_children,
            }
            private["input_digest"] = stable_hash(
                {
                    key: value
                    for key, value in private.items()
                    if key not in {"input_digest", "manifest"}
                }
                | {"manifest_digest": safe_document["manifest_digest"]}
            )
            return safe_document, private

    def manifest(
        self,
        operation_id: str,
        principal: Principal,
        target_identity: dict[str, Any],
    ) -> dict[str, Any]:
        document, _private = self._collect(
            operation_id, principal=principal, target_identity=target_identity
        )
        return {"manifest": document, "manifest_digest": document["manifest_digest"]}

    @staticmethod
    def _bundle_response(job: m.Job, result: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = job.payload_json if isinstance(job.payload_json, dict) else {}
        result = result if isinstance(result, dict) else {}
        status = str(job.status)
        if isinstance(result.get("publication_receipt"), dict):
            return {
                "status": "ready",
                "job_status": status,
                "recovered": status != "succeeded",
                "job_id": job.id,
                "manifest_digest": str(payload.get("manifest_digest") or ""),
                "artifact_id": result.get("artifact_id"),
                "manifest_artifact_id": result.get("manifest_artifact_id"),
                "sha256": result.get("sha256"),
                "size_bytes": result.get("size_bytes"),
                "manifest_sha256": result.get("manifest_sha256"),
                "manifest_size_bytes": result.get("manifest_size_bytes"),
                "content_url": _content_url(str(result.get("artifact_id") or "")),
                "manifest_content_url": _content_url(str(result.get("manifest_artifact_id") or "")),
            }
        return {
            "status": status,
            "job_status": status,
            "recovered": False,
            "job_id": job.id,
            "manifest_digest": str(payload.get("manifest_digest") or ""),
        }

    def _cached_job_valid(self, db, job: m.Job, input_digest: str) -> bool:
        if (
            job.kind != self.JOB_KIND
            or job.status
            not in {
                "queued",
                "running",
                "retrying",
                "cancel_requested",
                "succeeded",
                "failed",
                "canceled",
            }
            or not isinstance(job.payload_json, dict)
            or job.payload_json.get("input_digest") != input_digest
            or job.payload_json.get("bundle_job_id") != job.id
        ):
            return False
        result = job.result_json if isinstance(job.result_json, dict) else {}
        receipt = result.get("publication_receipt")
        if not isinstance(receipt, dict):
            return False
        payload = job.payload_json
        source_session_id = str(payload.get("source_session_id") or "")
        source_artifact_ids = sorted(
            {
                str(item.get("artifact_id"))
                for child in payload.get("languages", [])
                if isinstance(child, dict)
                for item in child.get("files", [])
                if isinstance(item, dict) and item.get("artifact_id")
            }
        )
        if (
            receipt.get("schema_version") != 1
            or receipt.get("job_id") != job.id
            or receipt.get("project_id") != payload.get("project_id")
            or receipt.get("source_operation_id") != payload.get("operation_id")
            or receipt.get("source_session_id") != source_session_id
            or receipt.get("input_digest") != input_digest
            or receipt.get("manifest_digest") != payload.get("manifest_digest")
            or receipt.get("source_artifact_ids") != source_artifact_ids
            or receipt.get("bundle_artifact_id") != result.get("artifact_id")
            or receipt.get("bundle_sha256") != result.get("sha256")
            or receipt.get("bundle_size_bytes") != result.get("size_bytes")
            or receipt.get("manifest_artifact_id") != result.get("manifest_artifact_id")
            or receipt.get("manifest_sha256") != result.get("manifest_sha256")
            or receipt.get("manifest_size_bytes") != result.get("manifest_size_bytes")
        ):
            return False
        if job.session_id != source_session_id:
            return False
        for artifact_id, role in (
            (result.get("artifact_id"), "project_export_bundle"),
            (result.get("manifest_artifact_id"), "project_export_manifest"),
        ):
            artifact = db.get(m.Artifact, str(artifact_id or ""))
            expected_hash = str(
                result.get("sha256")
                if role == "project_export_bundle"
                else result.get("manifest_sha256") or ""
            )
            expected_size = (
                result.get("size_bytes")
                if role == "project_export_bundle"
                else result.get("manifest_size_bytes")
            )
            if (
                artifact is None
                or artifact.session_id != source_session_id
                or artifact.state != "current"
                or artifact.role != role
                or artifact.content_hash != expected_hash
                or not expected_hash
                or (expected_size is not None and artifact.size_bytes != expected_size)
                or (artifact.metadata_json or {}).get("input_digest") != input_digest
                or (artifact.metadata_json or {}).get("project_id") != payload.get("project_id")
                or (artifact.metadata_json or {}).get("operation_id") != payload.get("operation_id")
                or (artifact.metadata_json or {}).get("source_operation_id")
                != payload.get("operation_id")
                or (artifact.metadata_json or {}).get("source_artifact_ids") != source_artifact_ids
                or (artifact.metadata_json or {}).get("manifest_digest")
                != payload.get("manifest_digest")
                or (artifact.metadata_json or {}).get("output_kind") != role
            ):
                return False
            try:
                _path, actual_size, digest = self._hash_file(artifact.relative_path)
            except (OSError, ValueError):
                return False
            if digest != expected_hash or (
                artifact.size_bytes is not None and actual_size != artifact.size_bytes
            ):
                return False
        return True

    def request_bundle(
        self,
        operation_id: str,
        expected_manifest_digest: str,
        *,
        principal: Principal,
        target_identity: dict[str, Any],
        idempotency_key: object,
    ) -> tuple[dict[str, Any], int]:
        key = self.services.idempotency.validate_key(idempotency_key)
        document, frozen = self._collect(
            operation_id, principal=principal, target_identity=target_identity
        )
        if not document["complete"]:
            raise ProjectExportBundleError(
                "bundle_incomplete",
                "Every selected language must have a verified completed export before bundling.",
                details={
                    "manifest_digest": document["manifest_digest"],
                    "incomplete_languages": [
                        item["language"]
                        for item in document["languages"]
                        if item["state"] == "blocked"
                        or item["operation_state"] not in {"completed", "existing"}
                    ],
                },
            )
        if not hmac.compare_digest(
            str(expected_manifest_digest or ""), document["manifest_digest"]
        ):
            raise ProjectExportBundleError(
                "manifest_digest_stale",
                "The export manifest changed. Refresh it before requesting a bundle.",
                details={"manifest_digest": document["manifest_digest"]},
            )

        request_payload = {
            "operation_id": operation_id,
            "manifest_digest": document["manifest_digest"],
            "input_digest": frozen["input_digest"],
            "target_identity": frozen["target_identity"],
        }
        with self.database.immediate_session() as db:
            reservation = self.services.idempotency.begin(
                db,
                principal=principal,
                operation_id=self.IDEMPOTENCY_OPERATION,
                idempotency_key=key,
                payload=request_payload,
            )
            if reservation.replayed:
                response, status_code = reservation.response or ({}, 202)
                job_id = str(response.get("job_id") or reservation.record.resource_id or "")
                job = db.get(m.Job, job_id) if job_id else None
                if job is not None and self._cached_job_valid(db, job, frozen["input_digest"]):
                    return self._bundle_response(job, job.result_json), 200
                if (
                    job is not None
                    and job.kind == self.JOB_KIND
                    and job.session_id == frozen["source_session_id"]
                    and isinstance(job.payload_json, dict)
                    and job.payload_json.get("input_digest") == frozen["input_digest"]
                    and job.status in {"queued", "running", "retrying", "cancel_requested"}
                ):
                    return self._bundle_response(job), 202
                if status_code == 200:
                    raise ProjectExportBundleError(
                        "bundle_cache_invalid",
                        "The cached bundle is no longer available; retry with a new idempotency key.",
                    )
                if (
                    job is not None
                    and job.kind == self.JOB_KIND
                    and isinstance(job.payload_json, dict)
                    and job.payload_json.get("input_digest") == frozen["input_digest"]
                ):
                    raise ProjectExportBundleError(
                        "bundle_job_terminal",
                        "The previous bundle job ended; retry with a new idempotency key.",
                        details={"job_id": job.id, "job_status": job.status},
                    )
                raise ProjectExportBundleError(
                    "bundle_job_unavailable",
                    "The recorded bundle job is unavailable; retry with a new idempotency key.",
                )

            recent_jobs = list(
                db.scalars(
                    select(m.Job)
                    .where(
                        m.Job.kind == self.JOB_KIND,
                        m.Job.session_id == frozen["source_session_id"],
                    )
                    .order_by(m.Job.created_at.desc())
                    .limit(200)
                ).all()
            )
            matching = next(
                (
                    job
                    for job in recent_jobs
                    if isinstance(job.payload_json, dict)
                    and job.payload_json.get("input_digest") == frozen["input_digest"]
                    and job.status
                    in {
                        "queued",
                        "running",
                        "retrying",
                        "cancel_requested",
                        "succeeded",
                        "failed",
                        "canceled",
                    }
                ),
                None,
            )
            if matching:
                if self._cached_job_valid(db, matching, frozen["input_digest"]):
                    response = self._bundle_response(matching, matching.result_json)
                    self.services.idempotency.complete(
                        db,
                        reservation,
                        response=response,
                        status_code=200,
                        resource_kind="job",
                        resource_id=matching.id,
                    )
                    return response, 200
                if matching.status not in {
                    "queued",
                    "running",
                    "retrying",
                    "cancel_requested",
                }:
                    matching = None
            if matching is not None:
                response = self._bundle_response(matching)
                self.services.idempotency.complete(
                    db,
                    reservation,
                    response=response,
                    status_code=202,
                    resource_kind="job",
                    resource_id=matching.id,
                )
                return response, 202

            job_payload = dict(frozen)
            job = self.services.jobs.enqueue_in_session(
                db,
                self.JOB_KIND,
                job_payload,
                session_id=frozen["source_session_id"],
                resource_keys=[f"project:{frozen['project_id']}:bundle"],
            )
            job.payload_json = {**job_payload, "bundle_job_id": job.id}
            response = {
                "status": "queued",
                "job_id": job.id,
                "manifest_digest": frozen["manifest_digest"],
            }
            self.services.idempotency.complete(
                db,
                reservation,
                response=response,
                status_code=202,
                resource_kind="job",
                resource_id=job.id,
            )
            return response, 202

    def _validate_frozen_inputs(
        self,
        db,
        payload: dict[str, Any],
        *,
        cancel_event: threading.Event | None = None,
    ) -> list[str]:
        bundle_job = self._validate_bundle_job(db, payload)
        del bundle_job
        operation = db.get(m.TranslationProjectOperation, payload["operation_id"])
        project = db.get(m.TranslationProject, payload["project_id"])
        if (
            operation is None
            or project is None
            or operation.principal_subject != payload["principal_subject"]
            or operation.target_instance_id != payload["target_identity"].get("instance_id")
            or not self._target_matches(operation, payload["target_identity"])
            or operation.action != "export"
            or operation.project_id != project.id
            or project.revision != payload["project_revision"]
            or stable_hash(project.name) != payload["project_name_fingerprint"]
            or project.source_session_id != payload["source_session_id"]
            or project.checkpoint_artifact_id != payload["source_checkpoint"]["artifact_id"]
            or project.source_content_hash != payload["source_checkpoint"]["sha256"]
            or int((operation.preview_json or {}).get("project_revision") or 0)
            != payload["project_revision"]
            or str((operation.preview_json or {}).get("export_kind") or "configured")
            != payload["export_kind"]
        ):
            raise ProjectExportBundleError(
                "bundle_inputs_changed",
                "The project or export operation changed before bundle publication.",
            )

        stored_children = operation.children_json or []
        frozen_by_branch = {row["branch_id"]: row for row in payload["languages"]}
        if len(stored_children) != len(frozen_by_branch) or {
            str(row.get("branch_id") or "") for row in stored_children
        } != set(frozen_by_branch):
            raise ProjectExportBundleError(
                "bundle_membership_changed",
                "Selected project branches changed before bundle publication.",
            )

        parent_ids: list[str] = []
        for branch_id, frozen_child in frozen_by_branch.items():
            _check_canceled(cancel_event)
            branch = db.get(m.TranslationProjectBranch, branch_id)
            session_record = db.get(m.SessionRecord, branch.session_id) if branch else None
            branch_checkpoint = (
                db.get(m.Artifact, branch.source_checkpoint_artifact_id) if branch else None
            )
            if (
                branch is None
                or branch.project_id != project.id
                or branch.session_id != frozen_child["session_id"]
                or canonical_language(branch.target_language) != frozen_child["language"]
                or branch.source_content_hash != payload["source_checkpoint"]["sha256"]
                or branch_checkpoint is None
                or branch_checkpoint.session_id != branch.session_id
                or branch_checkpoint.content_hash != payload["source_checkpoint"]["sha256"]
                or session_record is None
                or session_record.trashed_at is not None
            ):
                raise ProjectExportBundleError(
                    "bundle_membership_changed",
                    "A selected language branch changed before bundle publication.",
                )
            if frozen_child["operation_state"] not in {"completed", "existing"}:
                raise ProjectExportBundleError(
                    "bundle_inputs_changed",
                    "A selected export is no longer complete.",
                )
            job = db.get(m.Job, str(frozen_child.get("job_id") or ""))
            if job is None or job.session_id != branch.session_id or job.status != "succeeded":
                raise ProjectExportBundleError(
                    "bundle_inputs_changed",
                    "A selected export job is no longer in a completed state.",
                )
            if not frozen_child["files"]:
                raise ProjectExportBundleError(
                    "bundle_inputs_changed", "A selected export has no verified artifacts."
                )
            job_result = job.result_json if isinstance(job.result_json, dict) else {}
            job_artifact_ids = job_result.get("artifact_ids")
            if isinstance(job_artifact_ids, list):
                reported_ids = {str(item) for item in job_artifact_ids if item}
            else:
                reported_ids = (
                    {str(job_result["artifact_id"])} if job_result.get("artifact_id") else set()
                )
            frozen_ids = {str(item["artifact_id"]) for item in frozen_child["files"]}
            if reported_ids != frozen_ids:
                raise ProjectExportBundleError(
                    "bundle_inputs_changed",
                    "The completed export job's artifact receipts changed.",
                )
            for source_file in frozen_child["files"]:
                artifact = db.get(m.Artifact, source_file["artifact_id"])
                if (
                    artifact is None
                    or artifact.session_id != branch.session_id
                    or artifact.state != "current"
                    or not str(artifact.role or "").startswith("export")
                    or artifact.role != source_file["role"]
                    or artifact.kind != source_file["output_kind"]
                    or artifact.relative_path != source_file["relative_path"]
                    or str(artifact.content_hash or "").casefold() != source_file["sha256"]
                ):
                    raise ProjectExportBundleError(
                        "bundle_inputs_changed",
                        "An export artifact changed before bundle publication.",
                    )
                try:
                    _path, size, digest = self._hash_file(
                        artifact.relative_path, cancel_event=cancel_event
                    )
                except (OSError, ValueError) as error:
                    raise ProjectExportBundleError(
                        "bundle_inputs_changed",
                        "An export artifact is missing or unsafe before bundle publication.",
                    ) from error
                if digest != source_file["sha256"] or size != source_file["size_bytes"]:
                    raise ProjectExportBundleError(
                        "bundle_inputs_changed",
                        "An export artifact hash or size changed before bundle publication.",
                    )
                parent_ids.append(artifact.id)
        return sorted(set(parent_ids))

    def _validate_bundle_job(self, db, payload: dict[str, Any]) -> m.Job:
        stored_job_id = str(payload.get("bundle_job_id") or "")
        worker_job_id = str(payload.get("_job_id") or "")
        if not stored_job_id or (worker_job_id and worker_job_id != stored_job_id):
            raise ProjectExportBundleError(
                "bundle_job_changed", "The bundle job identity changed before publication."
            )
        job = db.get(m.Job, stored_job_id)
        stored_payload = job.payload_json if job and isinstance(job.payload_json, dict) else {}
        lease_generation = payload.get("_lease_generation")
        if (
            job is None
            or job.kind != self.JOB_KIND
            or job.session_id != payload.get("source_session_id")
            or job.status != "running"
            or not job.lease_owner
            or stored_payload.get("bundle_job_id") != job.id
            or stored_payload.get("input_digest") != payload.get("input_digest")
            or stored_payload.get("manifest_digest") != payload.get("manifest_digest")
            or stored_payload.get("operation_id") != payload.get("operation_id")
            or stored_payload.get("project_id") != payload.get("project_id")
            or (lease_generation is not None and int(lease_generation) != int(job.lease_generation))
        ):
            raise ProjectExportBundleError(
                "bundle_lease_lost",
                "The bundle job no longer has the active worker lease.",
            )
        return job

    @staticmethod
    def _zip_info(filename: str) -> zipfile.ZipInfo:
        info = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        info.flag_bits = 0
        return info

    def _output_parent(self, project_id: str) -> Path:
        parent = self.services.paths.artifacts / "project-exports" / project_id
        current = self.services.paths.root
        if current.is_symlink():
            raise ValueError("The managed bundle directory is unsafe.")
        for part in PurePosixPath(parent.relative_to(self.services.paths.root).as_posix()).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("The managed bundle directory is unsafe.")
            current.mkdir(exist_ok=True)
        self.services.paths.relative_managed_path(parent)
        return parent

    def _verify_published_directory(
        self,
        published: Path,
        document_bytes: bytes,
        files: list[dict[str, Any]],
        *,
        cancel_event: threading.Event | None,
    ) -> None:
        if published.is_symlink() or not published.is_dir():
            raise ProjectExportBundleError(
                "bundle_publication_invalid", "The deterministic bundle directory is unsafe."
            )
        if stat.S_IMODE(published.lstat().st_mode) & 0o077:
            raise ProjectExportBundleError(
                "bundle_publication_invalid", "The deterministic bundle directory is not private."
            )
        self.services.paths.relative_managed_path(published)
        expected_names = ["manifest.json", *[item["filename"] for item in files]]
        if len(expected_names) != len(set(expected_names)):
            raise ProjectExportBundleError(
                "bundle_publication_invalid", "The frozen bundle contains duplicate filenames."
            )
        directory_names = {entry.name for entry in published.iterdir()}
        if directory_names != {"bundle.zip", "manifest.json"}:
            raise ProjectExportBundleError(
                "bundle_publication_invalid",
                "The deterministic bundle directory contains unexpected files.",
            )
        bundle_path = published / "bundle.zip"
        manifest_path = published / "manifest.json"
        for path in (bundle_path, manifest_path):
            try:
                mode = path.lstat().st_mode
            except OSError as error:
                raise ProjectExportBundleError(
                    "bundle_publication_invalid", "A published bundle file is unavailable."
                ) from error
            if not stat.S_ISREG(mode):
                raise ProjectExportBundleError(
                    "bundle_publication_invalid", "Published bundle files must be regular files."
                )
        if manifest_path.stat().st_size != len(document_bytes):
            raise ProjectExportBundleError(
                "bundle_publication_invalid", "The published manifest bytes do not match."
            )
        with manifest_path.open("rb") as handle:
            _check_canceled(cancel_event)
            if handle.read(len(document_bytes) + 1) != document_bytes:
                raise ProjectExportBundleError(
                    "bundle_publication_invalid", "The published manifest bytes do not match."
                )

        expected_by_name = {item["filename"]: item for item in files}
        try:
            with zipfile.ZipFile(bundle_path, "r") as archive:
                infos = archive.infolist()
                if [info.filename for info in infos] != expected_names:
                    raise ProjectExportBundleError(
                        "bundle_publication_invalid",
                        "The published archive member list does not match.",
                    )
                for info in infos:
                    _check_canceled(cancel_event)
                    if (
                        info.is_dir()
                        or (info.external_attr >> 16) != 0o100644
                        or info.date_time != (1980, 1, 1, 0, 0, 0)
                        or info.compress_type != zipfile.ZIP_DEFLATED
                    ):
                        raise ProjectExportBundleError(
                            "bundle_publication_invalid",
                            "The published archive member metadata is invalid.",
                        )
                    digest = hashlib.sha256()
                    size = 0
                    manifest_content = bytearray() if info.filename == "manifest.json" else None
                    with archive.open(info, "r") as source:
                        while chunk := source.read(_CHUNK_SIZE):
                            _check_canceled(cancel_event)
                            digest.update(chunk)
                            size += len(chunk)
                            if manifest_content is not None:
                                manifest_content.extend(chunk)
                    if info.filename == "manifest.json":
                        if bytes(manifest_content or b"") != document_bytes:
                            raise ProjectExportBundleError(
                                "bundle_publication_invalid",
                                "The embedded manifest bytes do not match.",
                            )
                        continue
                    expected = expected_by_name[info.filename]
                    if digest.hexdigest() != expected["sha256"] or size != int(
                        expected["size_bytes"]
                    ):
                        raise ProjectExportBundleError(
                            "bundle_publication_invalid",
                            "A published archive member does not match its source artifact.",
                        )
        except (OSError, zipfile.BadZipFile, RuntimeError) as error:
            if isinstance(error, ProjectExportBundleError):
                raise
            raise ProjectExportBundleError(
                "bundle_publication_invalid", "The published ZIP could not be verified."
            ) from error

    def _publication_result(
        self,
        db,
        payload: dict[str, Any],
        published: Path,
        parent_ids: list[str],
    ) -> dict[str, Any]:
        source_artifact_ids = sorted(set(parent_ids))
        bundle_path = published / "bundle.zip"
        manifest_path = published / "manifest.json"
        bundle_prepared = self.services.artifacts.prepare_registration(
            bundle_path, calculate_hash=True
        )
        manifest_prepared = self.services.artifacts.prepare_registration(
            manifest_path, calculate_hash=True
        )
        expected_metadata = {
            "project_id": payload["project_id"],
            "operation_id": payload["operation_id"],
            "source_operation_id": payload["operation_id"],
            "source_artifact_ids": source_artifact_ids,
            "input_digest": payload["input_digest"],
            "manifest_digest": payload["manifest_digest"],
        }
        all_outputs = list(
            db.scalars(
                select(m.Artifact).where(
                    m.Artifact.session_id == payload["source_session_id"],
                    m.Artifact.role.in_(("project_export_bundle", "project_export_manifest")),
                )
            ).all()
        )
        matching = [
            artifact
            for artifact in all_outputs
            if (artifact.metadata_json or {}).get("input_digest") == payload["input_digest"]
        ]
        reused: dict[str, m.Artifact] = {}
        if matching:
            for role, kind, prepared in (
                ("project_export_bundle", "zip", bundle_prepared),
                ("project_export_manifest", "json", manifest_prepared),
            ):
                rows = [artifact for artifact in matching if artifact.role == role]
                if len(rows) != 1:
                    raise ProjectExportBundleError(
                        "bundle_publication_inconsistent",
                        "The deterministic bundle already has an incomplete artifact pair.",
                    )
                artifact = rows[0]
                metadata = artifact.metadata_json or {}
                if (
                    artifact.state != "current"
                    or artifact.kind != kind
                    or artifact.relative_path != prepared.relative_path
                    or artifact.content_hash != prepared.content_hash
                    or artifact.size_bytes != prepared.size_bytes
                    or any(metadata.get(key) != value for key, value in expected_metadata.items())
                    or metadata.get("output_kind") != role
                ):
                    raise ProjectExportBundleError(
                        "bundle_publication_inconsistent",
                        "The existing bundle artifact receipt does not match the publication.",
                    )
                reused[role] = artifact
        if reused:
            bundle_artifact = reused["project_export_bundle"]
            manifest_artifact = reused["project_export_manifest"]
        else:
            bundle_artifact = self.services.artifacts.register_in_session(
                db,
                bundle_path,
                kind="zip",
                role="project_export_bundle",
                session_id=payload["source_session_id"],
                parent_ids=source_artifact_ids,
                metadata={**expected_metadata, "output_kind": "project_export_bundle"},
                _prepared=replace(
                    bundle_prepared,
                    relative_path=self.services.paths.relative_managed_path(bundle_path),
                ),
            )
            manifest_artifact = self.services.artifacts.register_in_session(
                db,
                manifest_path,
                kind="json",
                role="project_export_manifest",
                session_id=payload["source_session_id"],
                parent_ids=source_artifact_ids,
                metadata={**expected_metadata, "output_kind": "project_export_manifest"},
                _prepared=replace(
                    manifest_prepared,
                    relative_path=self.services.paths.relative_managed_path(manifest_path),
                ),
            )
        db.flush()
        result = {
            "artifact_id": bundle_artifact.id,
            "manifest_artifact_id": manifest_artifact.id,
            "sha256": bundle_artifact.content_hash,
            "size_bytes": bundle_artifact.size_bytes,
            "manifest_sha256": manifest_artifact.content_hash,
            "manifest_size_bytes": manifest_artifact.size_bytes,
            "manifest_digest": payload["manifest_digest"],
            "input_digest": payload["input_digest"],
            "content_url": _content_url(bundle_artifact.id),
            "manifest_content_url": _content_url(manifest_artifact.id),
        }
        result["publication_receipt"] = {
            "schema_version": 1,
            "job_id": payload["bundle_job_id"],
            "project_id": payload["project_id"],
            "source_operation_id": payload["operation_id"],
            "source_session_id": payload["source_session_id"],
            "source_artifact_ids": source_artifact_ids,
            "input_digest": payload["input_digest"],
            "manifest_digest": payload["manifest_digest"],
            "bundle_artifact_id": bundle_artifact.id,
            "bundle_sha256": bundle_artifact.content_hash,
            "bundle_size_bytes": bundle_artifact.size_bytes,
            "manifest_artifact_id": manifest_artifact.id,
            "manifest_sha256": manifest_artifact.content_hash,
            "manifest_size_bytes": manifest_artifact.size_bytes,
        }
        return result

    def _publication_paths_registered(self, published: Path) -> bool:
        try:
            relative_paths = (
                self.services.paths.relative_managed_path(published / "bundle.zip"),
                self.services.paths.relative_managed_path(published / "manifest.json"),
            )
            with self.database.snapshot_session() as db:
                return (
                    db.scalar(
                        select(m.Artifact.id)
                        .where(m.Artifact.relative_path.in_(relative_paths))
                        .limit(1)
                    )
                    is not None
                )
        except Exception:
            # An ambiguous database outcome must preserve possibly registered files.
            return True

    def generate(
        self,
        payload: dict[str, Any],
        progress: ProgressCallback,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        """Publish or recover an immutable bundle under the active job lease."""

        if not isinstance(payload, dict):
            raise TypeError("Project export bundle payload must be an object.")
        _check_canceled(cancel_event)
        document = payload.get("manifest")
        input_digest = str(payload.get("input_digest") or "")
        if (
            not isinstance(document, dict)
            or document.get("manifest_digest") != payload.get("manifest_digest")
            or _manifest_digest(document) != payload.get("manifest_digest")
            or not document.get("complete")
            or not _HASH.fullmatch(input_digest)
            or not _HASH.fullmatch(str(payload.get("manifest_digest") or ""))
            or not str(payload.get("bundle_job_id") or "")
        ):
            raise ProjectExportBundleError(
                "bundle_payload_invalid", "The frozen bundle payload failed validation."
            )

        # A committed receipt survives a worker crash after publication. Verify the
        # frozen inputs and output artifacts before returning it to a retried worker.
        with self.database.snapshot_session() as db:
            self._validate_frozen_inputs(db, payload, cancel_event=cancel_event)
            bundle_job = self._validate_bundle_job(db, payload)
            if self._cached_job_valid(db, bundle_job, input_digest):
                cached_result = bundle_job.result_json
                if isinstance(cached_result, dict):
                    return dict(cached_result)

        parent = self._output_parent(str(payload["project_id"]))
        published = parent / input_digest
        self.services.paths.relative_managed_path(published)
        staging = Path(tempfile.mkdtemp(prefix=".bundle-stage-", dir=parent))
        committed = False
        published_created = False
        abrupt_termination = False
        zip_staged = staging / "bundle.zip"
        manifest_staged = staging / "manifest.json"
        document_bytes = (
            json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        try:
            _check_canceled(cancel_event)
            manifest_staged.write_bytes(document_bytes)
            files = [item for child in payload["languages"] for item in child["files"]]
            files.sort(key=lambda item: (item["language"], item["filename"], item["artifact_id"]))
            total = sum(max(1, int(item["size_bytes"])) for item in files)
            copied = 0
            with zipfile.ZipFile(
                zip_staged,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
                allowZip64=True,
            ) as archive:
                manifest_info = self._zip_info("manifest.json")
                manifest_info.file_size = len(document_bytes)
                with archive.open(manifest_info, "w", force_zip64=True) as output:
                    output.write(document_bytes)
                for index, item in enumerate(files, start=1):
                    _check_canceled(cancel_event)
                    if (
                        not _LANGUAGE_FILENAME.fullmatch(str(item["language"]))
                        or PurePosixPath(str(item["filename"])).name != item["filename"]
                        or item["filename"] in {"", "manifest.json"}
                    ):
                        raise ProjectExportBundleError(
                            "bundle_filename_invalid", "A frozen bundle filename is unsafe."
                        )
                    _path, source = self._open_managed_regular_file(item["relative_path"])
                    info = self._zip_info(str(item["filename"]))
                    info.file_size = int(item["size_bytes"])
                    with source, archive.open(info, "w", force_zip64=True) as destination:
                        digest = hashlib.sha256()
                        size = 0
                        while chunk := source.read(_CHUNK_SIZE):
                            _check_canceled(cancel_event)
                            destination.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                            copied += len(chunk)
                            if progress and copied % (8 * _CHUNK_SIZE) < len(chunk):
                                progress(
                                    min(0.88, 0.1 + 0.78 * copied / max(1, total)),
                                    f"Packing language export {index} of {len(files)}",
                                )
                    if digest.hexdigest() != item["sha256"] or size != int(item["size_bytes"]):
                        raise ProjectExportBundleError(
                            "bundle_inputs_changed",
                            "An export artifact changed while the bundle was being written.",
                        )
                    if progress:
                        progress(
                            min(0.88, 0.1 + 0.78 * copied / max(1, total)),
                            f"Packing language export {index} of {len(files)}",
                        )

            _check_canceled(cancel_event)
            if progress:
                progress(0.9, "Verifying frozen export inputs")

            with self.database.immediate_session() as db:
                parent_ids = self._validate_frozen_inputs(db, payload, cancel_event=cancel_event)
                bundle_job = self._validate_bundle_job(db, payload)
                if self._cached_job_valid(db, bundle_job, input_digest):
                    cached_result = bundle_job.result_json
                    if not isinstance(cached_result, dict):
                        raise ProjectExportBundleError(
                            "bundle_receipt_invalid",
                            "The committed bundle receipt is unavailable.",
                        )
                    result = dict(cached_result)
                else:
                    if published.exists() or published.is_symlink():
                        self._verify_published_directory(
                            published,
                            document_bytes,
                            files,
                            cancel_event=cancel_event,
                        )
                    else:
                        os.replace(staging, published)
                        published_created = True
                        self._verify_published_directory(
                            published,
                            document_bytes,
                            files,
                            cancel_event=cancel_event,
                        )
                    result = self._publication_result(db, payload, published, parent_ids)
                    bundle_job.result_json = result
                    bundle_job.updated_at = m.utcnow()
                    db.flush()
            committed = True
            if progress:
                progress(1.0, "Project export bundle ready")
            return result
        except BaseException as error:
            abrupt_termination = not isinstance(error, Exception)
            raise
        finally:
            # Exception failures roll back newly published files. BaseException
            # models worker-process death, where finally would not normally run.
            if not abrupt_termination:
                if (
                    not committed
                    and published_created
                    and not self._publication_paths_registered(published)
                ):
                    shutil.rmtree(published, ignore_errors=True)
                shutil.rmtree(staging, ignore_errors=True)
