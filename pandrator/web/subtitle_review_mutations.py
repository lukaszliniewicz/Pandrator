"""Own subtitle review publication, transaction completion and retry identity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .auth import Principal
from .database import Database
from .idempotency import IdempotencyService
from .schemas import SubtitlePassageReviewRequest, SubtitleReviewRequest
from .subtitle_review import SubtitleReviewService, cleanup_review_publications


@dataclass(frozen=True, slots=True)
class SubtitleMutationResult:
    payload: dict[str, Any]
    status_code: int = 201
    replayed: bool = False


class SubtitleReviewMutationService:
    """Borrow domain services; keep filesystem rollback with the DB owner."""

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
                **payload.model_dump(mode="json"),
            },
            immediate=idempotency_key is not None,
            mutate=lambda session, paths: self.review.save_review_in_session(
                session,
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.segments],
                source_artifact_id=payload.source_artifact_id,
                expected_source_hash=payload.expected_source_hash,
                published_paths=paths,
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
                **payload.model_dump(mode="json"),
            },
            immediate=True,
            mutate=lambda session, paths: self.review.save_passage_review_in_session(
                session,
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.passages],
                source_artifact_id=payload.source_artifact_id,
                expected_source_hash=payload.expected_source_hash,
                expected_composition_hash=payload.expected_composition_hash,
                published_paths=paths,
            ),
        )

    def _save(
        self,
        *,
        operation_id: str,
        principal: Principal,
        idempotency_key: str | None,
        request_payload: dict[str, Any],
        immediate: bool,
        mutate: Callable[[Session, list[Path]], dict[str, Any]],
    ) -> SubtitleMutationResult:
        published_paths: list[Path] = []
        transaction = self.database.immediate_session if immediate else self.database.session
        try:
            with transaction() as session:
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
                result = mutate(session, published_paths)
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
