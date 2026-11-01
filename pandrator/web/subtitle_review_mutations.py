"""Own subtitle review publication, transaction completion and retry identity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .auth import Principal
from .database import Database
from .idempotency import IdempotencyService
from .schemas import SubtitlePassageReviewRequest, SubtitleReviewRequest
from .subtitle_review import (
    PreparedSubtitleReview,
    SubtitleReviewService,
    cleanup_review_publications,
)


@dataclass(frozen=True, slots=True)
class SubtitleMutationResult:
    payload: dict[str, Any]
    status_code: int = 201
    replayed: bool = False


class SubtitleReviewMutationService:
    """Prepare outside the writer; keep publication and retry identity atomic."""

    def __init__(
        self,
        database: Database,
        idempotency: IdempotencyService,
        review: SubtitleReviewService,
    ):
        self.database = database
        self.idempotency = idempotency
        self.review = review

    def save_review(
        self,
        session_id: str,
        stage: str,
        payload: SubtitleReviewRequest,
        *,
        principal: Principal,
        idempotency_key: str | None,
    ) -> SubtitleMutationResult:
        return self._save(
            operation_id="saveSubtitleReview",
            principal=principal,
            idempotency_key=idempotency_key,
            request_payload={
                "session_id": session_id,
                "stage": stage,
                **payload.model_dump(mode="json", exclude={"dry_run"}),
            },
            dry_run=payload.dry_run,
            prepare=lambda: self.review.prepare_review(
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.segments],
                dry_run=payload.dry_run,
                source_artifact_id=payload.source_artifact_id,
                expected_source_hash=payload.expected_source_hash,
            ),
        )

    def save_passage_review(
        self,
        session_id: str,
        stage: str,
        payload: SubtitlePassageReviewRequest,
        *,
        principal: Principal,
        idempotency_key: str | None,
    ) -> SubtitleMutationResult:
        return self._save(
            operation_id="saveSubtitlePassageReview",
            principal=principal,
            idempotency_key=idempotency_key,
            request_payload={
                "session_id": session_id,
                "stage": stage,
                **payload.model_dump(mode="json", exclude={"dry_run"}),
            },
            dry_run=payload.dry_run,
            prepare=lambda: self.review.prepare_passage_review(
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.passages],
                dry_run=payload.dry_run,
                source_artifact_id=payload.source_artifact_id,
                expected_source_hash=payload.expected_source_hash,
                expected_composition_hash=payload.expected_composition_hash,
            ),
        )

    def _save(
        self,
        *,
        operation_id: str,
        principal: Principal,
        idempotency_key: str | None,
        request_payload: dict[str, Any],
        dry_run: bool,
        prepare: Callable[[], PreparedSubtitleReview | dict[str, Any]],
    ) -> SubtitleMutationResult:
        session_id = str(request_payload["session_id"])
        if not dry_run and idempotency_key is not None:
            with self.database.snapshot_session() as session:
                require_current_session_access(session, session_id, ProjectAction.EDIT)
                reservation = self.idempotency.replay_if_present(
                    session, principal=principal, operation_id=operation_id,
                    idempotency_key=idempotency_key, payload=request_payload,
                )
                if reservation is not None and reservation.response is not None:
                    result, status_code = reservation.response
                    return SubtitleMutationResult(result, status_code, replayed=True)
        prepared = prepare()
        if isinstance(prepared, dict):
            return SubtitleMutationResult(prepared, status_code=200)
        published_paths: list[Path] = []
        try:
            with self.database.immediate_session() as session:
                require_current_session_access(session, session_id, ProjectAction.EDIT)
                reservation = (
                    self.idempotency.begin(
                        session,
                        principal=principal,
                        operation_id=operation_id,
                        idempotency_key=idempotency_key,
                        payload=request_payload,
                    )
                    if idempotency_key is not None
                    else None
                )
                if reservation is not None and reservation.response is not None:
                    result, status_code = reservation.response
                    return SubtitleMutationResult(result, status_code, replayed=True)
                result = self.review.publish_prepared_review_in_session(
                    session, prepared, published_paths,
                )
                if reservation is not None:
                    self.idempotency.complete(
                        session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="subtitle_document",
                        resource_id=str(result["document_id"]),
                    )
            return SubtitleMutationResult(result)
        except BaseException:
            cleanup_review_publications(published_paths)
            raise
        finally:
            prepared.cleanup()
