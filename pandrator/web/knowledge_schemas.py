"""Bounded HTTP inputs and explicit materialized/empty ledger reads."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_language: str = Field(default="auto", min_length=1, max_length=40)
    target_language: str = Field(default="", max_length=40)


class KnowledgeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    payload: dict[str, Any]


class KnowledgeLedgerResponse(BaseModel):
    id: str | None
    session_id: str
    kind: Literal["research", "glossary"]
    source_language: str
    target_language: str
    payload: dict[str, Any]
    revision: int = Field(ge=0)
    updated_at: datetime | None


class KnowledgeUpdateResponse(BaseModel):
    id: str
    payload: dict[str, Any]
    revision: int = Field(ge=1)
    updated_at: datetime


KNOWLEDGE_SCHEMAS = {
    model.__name__: model
    for model in (
        KnowledgeScope,
        KnowledgeUpdateRequest,
        KnowledgeLedgerResponse,
        KnowledgeUpdateResponse,
    )
}
