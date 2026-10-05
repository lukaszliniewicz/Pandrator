"""Claim-fenced publication of staged XTTS training bundles."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC
from pathlib import Path
from threading import Event
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.xtts_model_paths import (
    promote_training_model_directory,
    resolve_training_model_target,
)
from pandrator.logic.xtts_trainer_handler import XTTS_MODEL_BUNDLE_FILENAMES
from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .database import Database
from .models import Artifact, Job, TrainingRun, utcnow
from .training_state import TRAINING_ACTIVE_STATUSES, is_training_job_owner

logger = logging.getLogger(__name__)
_MANIFEST_FILENAME = "pandrator-training.json"


def require_training_claim(
    session: Session,
    payload: dict[str, Any],
    cancel_event: Event,
    *,
    allow_cancel: bool = False,
) -> TrainingRun:
    """Refuse stale workers before they mutate a training run or publish files."""
    training = session.get(
        TrainingRun, str(payload.get("training_id") or ""), populate_existing=True
    )
    if training is None:
        raise ValueError("Training record not found.")
    if (
        payload.get("model_name") != training.model_name
        or payload.get("source_artifact_id") != training.source_artifact_id
        or (payload.get("source_text_artifact_id") or None) != training.source_text_artifact_id
        or (payload.get("settings") or {}) != (training.settings_json or {})
    ):
        raise InterruptedError("Training inputs no longer match this worker.")
    if training.status not in TRAINING_ACTIVE_STATUSES:
        raise InterruptedError("Training run is no longer active.")
    has_job_id = "_job_id" in payload
    has_generation = "_lease_generation" in payload
    if not has_job_id and not has_generation:
        if training.job_id is not None:
            raise InterruptedError("Training worker has no current job claim.")
    else:
        generation = payload.get("_lease_generation")
        if not has_job_id or not has_generation or type(generation) is not int:
            raise InterruptedError("Training worker has an incomplete job claim.")
        job = session.get(Job, payload.get("_job_id"), populate_existing=True)
        allowed_statuses = {"running", "cancel_requested"} if allow_cancel else {"running"}
        if (
            not is_training_job_owner(training, job)
            or job is None
            or job.lease_generation != generation
            or job.lease_owner is None
            or job.lease_expires_at is None
            or job.lease_expires_at.replace(tzinfo=UTC) <= utcnow()
            or job.status not in allowed_statuses
        ):
            raise InterruptedError("Training worker no longer owns a current job claim.")
    if not allow_cancel and (cancel_event.is_set() or training.status == "cancel_requested"):
        raise InterruptedError("Training cancellation was requested.")
    return training


def transition_training(
    database: Database,
    payload: dict[str, Any],
    cancel_event: Event,
    *,
    status: str,
    error_message: str | None = None,
) -> None:
    """Update a current attempt in one serialized writer."""
    with database.immediate_session() as session:
        training = require_training_claim(
            session, payload, cancel_event, allow_cancel=status == "canceled"
        )
        training.status = status
        training.error_message = error_message
        training.updated_at = utcnow()


def _require_no_symlinks(path: Path, root: Path) -> None:
    try:
        parts = path.absolute().relative_to(root.absolute()).parts
    except ValueError as error:
        raise ValueError("Training publication path is outside the managed root.") from error
    current = root
    for part in ("", *parts):
        current = current / part
        if current.is_symlink():
            raise ValueError("Training publication paths must not contain symlinks.")


def _restore_owned_publication(
    target: Path,
    staging: Path,
    identity: tuple[int, int],
    manifest_bytes: bytes,
    nonce: str,
) -> None:
    """Recover only this attempt's unchanged directory after a normal rollback."""
    if staging.exists() or staging.is_symlink():
        raise RuntimeError("Training staging path became occupied during rollback.")
    if target.is_symlink() or not target.is_dir():
        raise RuntimeError("Published training directory changed during rollback.")
    stat = target.stat()
    manifest = target / _MANIFEST_FILENAME
    if (
        (stat.st_dev, stat.st_ino) != identity
        or manifest.is_symlink()
        or manifest.read_bytes() != manifest_bytes
        or json.loads(manifest_bytes).get("publication_nonce") != nonce
    ):
        raise RuntimeError("Published training ownership proof changed during rollback.")
    promote_training_model_directory(target, staging)


def publish_training_bundle(
    database: Database,
    paths: DataPaths,
    artifacts: ArtifactService,
    payload: dict[str, Any],
    cancel_event: Event,
    staging: Path,
    target: Path,
) -> dict[str, Any]:
    """Prepare outside the writer, then publish files and their records together."""
    model_name = str(payload.get("model_name") or "")
    _require_no_symlinks(paths.models / "xtts", paths.root)
    model_root, expected_target = resolve_training_model_target(model_name, paths.models / "xtts")
    if target.absolute() != expected_target:
        raise ValueError("Training target does not match the managed model name.")
    _require_no_symlinks(target, model_root)
    _require_no_symlinks(staging, model_root)
    if (
        staging.parent.absolute() != model_root / ".downloads"
        or not staging.name.startswith(".training-")
        or not staging.is_dir()
        or {path.name for path in staging.iterdir()} != set(XTTS_MODEL_BUNDLE_FILENAMES)
    ):
        raise ValueError("Training bundle must be complete and privately staged.")
    for filename in XTTS_MODEL_BUNDLE_FILENAMES:
        path = staging / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError("Training bundle contains an unsafe or missing model file.")
    training_id = str(payload.get("training_id") or "")
    settings = dict(payload.get("settings") or {})
    nonce = uuid.uuid4().hex
    message = f"Trained model '{model_name}' published to {target}"
    manifest_data = {
        "kind": "xtts",
        "model_name": model_name,
        "message": message,
        "training_id": training_id,
        "source_artifact_id": payload.get("source_artifact_id"),
        "source_text_artifact_id": payload.get("source_text_artifact_id") or None,
        "publication_nonce": nonce,
        "bundle_sha256": {
            filename: sha256_file(staging / filename) for filename in XTTS_MODEL_BUNDLE_FILENAMES
        },
    }
    manifest_bytes = (json.dumps(manifest_data, indent=2) + "\n").encode("utf-8")
    staged_manifest = staging / _MANIFEST_FILENAME
    with staged_manifest.open("xb") as output:
        output.write(manifest_bytes)
    final_manifest = target / _MANIFEST_FILENAME
    prepared = replace(
        artifacts.prepare_registration(staged_manifest, settings=settings),
        relative_path=paths.relative_managed_path(final_manifest),
    )
    stat = staging.stat()
    identity = (stat.st_dev, stat.st_ino)
    parent_ids = [str(payload.get("source_artifact_id") or "")]
    if payload.get("source_text_artifact_id"):
        parent_ids.append(str(payload["source_text_artifact_id"]))
    target.parent.mkdir(parents=True, exist_ok=True)
    promoted = False
    try:
        with database.immediate_session() as session:
            require_training_claim(session, payload, cancel_event)
            if (
                session.scalar(
                    select(Artifact.id).where(Artifact.relative_path == prepared.relative_path)
                )
                is not None
            ):
                raise RuntimeError("Training target already has an artifact registration.")
            artifact = artifacts.register_in_session(
                session,
                final_manifest,
                kind="model",
                role="xtts_model",
                parent_ids=parent_ids,
                settings=settings,
                metadata={"model_name": model_name},
                _prepared=prepared,
            )
            session.flush()
            training = require_training_claim(session, payload, cancel_event)
            promote_training_model_directory(staging, target)
            promoted = True
            training.status = "succeeded"
            training.output_artifact_id = artifact.id
            training.error_message = None
            training.updated_at = utcnow()
            result = {
                "training_id": training_id,
                "artifact_id": artifact.id,
                "model_name": model_name,
                "message": message,
            }
        return result
    except BaseException:
        if promoted:
            try:
                _restore_owned_publication(target, staging, identity, manifest_bytes, nonce)
            except BaseException:
                logger.warning(
                    "Could not recover training publication at %s after rollback; "
                    "preserving the residual directory for inspection.",
                    target,
                    exc_info=True,
                )
        raise
