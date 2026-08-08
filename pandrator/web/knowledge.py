"""Versioned structured research and glossary ledgers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, ClassVar

from sqlalchemy import select

from .database import Database
from .models import KnowledgeLedger, utcnow


def _normalized(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _empty_payload(kind: str) -> dict[str, Any]:
    return (
        {"entries": [], "conflicts": []}
        if kind == "glossary"
        else {
            "evidence": [],
            "warnings": [],
            "summary": "",
        }
    )


class KnowledgeValidationError(ValueError):
    """A client-supplied knowledge ledger does not match its JSON contract."""


class KnowledgeLedgerStore:
    KINDS: ClassVar[set[str]] = {"research", "glossary"}

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _validated_payload(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        raw = deepcopy(dict(payload))
        if kind == "research":
            evidence = raw.get("evidence", [])
            warnings = raw.get("warnings", [])
            if not isinstance(evidence, list) or not all(
                isinstance(item, Mapping) for item in evidence
            ):
                raise KnowledgeValidationError(
                    "Research evidence must be a list of JSON objects."
                )
            if not isinstance(warnings, list):
                raise KnowledgeValidationError("Research warnings must be a list.")
            return {
                "evidence": [dict(item) for item in evidence],
                "warnings": [" ".join(str(item).split()) for item in warnings],
                "summary": " ".join(str(raw.get("summary") or "").split()),
            }
        entries = raw.get("entries", [])
        conflicts = raw.get("conflicts", [])
        if not isinstance(entries, list) or not all(
            isinstance(item, Mapping) for item in entries
        ):
            raise KnowledgeValidationError(
                "Glossary entries must be a list of JSON objects."
            )
        if not isinstance(conflicts, list) or not all(
            isinstance(item, Mapping) for item in conflicts
        ):
            raise KnowledgeValidationError(
                "Glossary conflicts must be a list of JSON objects."
            )
        normalized_entries: list[dict[str, Any]] = []
        for item in entries:
            record = dict(item)
            source = " ".join(str(record.get("source") or "").split())
            target = " ".join(str(record.get("target") or "").split())
            if not source or not target:
                raise KnowledgeValidationError(
                    "Every glossary entry needs source and target text."
                )
            normalized_entries.append(
                {
                    **record,
                    "source": source,
                    "target": target,
                    "locked": bool(record.get("locked")),
                    "status": str(record.get("status") or "active"),
                }
            )
        return {
            "entries": normalized_entries,
            "conflicts": [dict(item) for item in conflicts],
        }

    def get(
        self,
        session_id: str,
        kind: str,
        *,
        source_language: str = "auto",
        target_language: str = "",
    ) -> dict[str, Any]:
        normalized_kind = str(kind).strip().lower()
        if normalized_kind not in self.KINDS:
            raise ValueError(f"Unsupported knowledge ledger: {kind}")
        source = str(source_language or "auto").strip().lower()
        target = str(target_language or "").strip().lower()
        with self.database.session() as session:
            record = session.scalar(
                select(KnowledgeLedger).where(
                    KnowledgeLedger.session_id == session_id,
                    KnowledgeLedger.kind == normalized_kind,
                    KnowledgeLedger.source_language == source,
                    KnowledgeLedger.target_language == target,
                )
            )
            if record is None:
                record = KnowledgeLedger(
                    session_id=session_id,
                    kind=normalized_kind,
                    source_language=source,
                    target_language=target,
                    payload_json=_empty_payload(normalized_kind),
                )
                session.add(record)
                session.flush()
            return {
                "id": record.id,
                "session_id": record.session_id,
                "kind": record.kind,
                "source_language": record.source_language,
                "target_language": record.target_language,
                "payload": deepcopy(record.payload_json or {}),
                "revision": record.revision,
                "updated_at": record.updated_at.isoformat(),
            }

    def replace(
        self, ledger_id: str, expected_revision: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self.database.session() as session:
            record = session.get(KnowledgeLedger, ledger_id)
            if record is None:
                raise KeyError(ledger_id)
            if int(expected_revision) != record.revision:
                raise ValueError("The knowledge ledger changed in another client.")
            record.payload_json = self._validated_payload(record.kind, payload)
            record.revision += 1
            record.updated_at = utcnow()
            return {
                "id": record.id,
                "payload": deepcopy(record.payload_json),
                "revision": record.revision,
                "updated_at": record.updated_at.isoformat(),
            }

    def merge_research(
        self,
        session_id: str,
        *,
        source_language: str,
        target_language: str,
        evidence: Iterable[Mapping[str, Any]],
        warnings: Iterable[str] = (),
        summary: str = "",
    ) -> dict[str, Any]:
        current = self.get(
            session_id,
            "research",
            source_language=source_language,
            target_language=target_language,
        )
        payload = deepcopy(current["payload"])
        existing = {
            (
                _normalized(item.get("recommendation")),
                _normalized(item.get("source_url")),
            )
            for item in payload.get("evidence", [])
            if isinstance(item, Mapping)
        }
        for item in evidence:
            record = dict(item)
            key = (
                _normalized(record.get("recommendation")),
                _normalized(record.get("source_url")),
            )
            if not key[0] or key in existing:
                continue
            record.setdefault("status", "verified")
            payload.setdefault("evidence", []).append(record)
            existing.add(key)
        for warning in warnings:
            value = " ".join(str(warning).split())
            if value and value not in payload.setdefault("warnings", []):
                payload["warnings"].append(value)
        if summary:
            payload["summary"] = " ".join(str(summary).split())
        return self.replace(current["id"], current["revision"], payload)

    def merge_glossary(
        self,
        session_id: str,
        *,
        source_language: str,
        target_language: str,
        entries: Iterable[Mapping[str, Any]],
        origin: str,
        locked: bool = False,
    ) -> dict[str, Any]:
        current = self.get(
            session_id,
            "glossary",
            source_language=source_language,
            target_language=target_language,
        )
        payload = deepcopy(current["payload"])
        records = [
            dict(item)
            for item in payload.get("entries", [])
            if isinstance(item, Mapping)
        ]
        positions = {
            _normalized(item.get("source")): index for index, item in enumerate(records)
        }
        for raw in entries:
            source = " ".join(str(raw.get("source") or "").split())
            target = " ".join(str(raw.get("target") or "").split())
            if not source or not target:
                continue
            key = _normalized(source)
            proposed = {
                **dict(raw),
                "source": source,
                "target": target,
                "origin": origin,
                "locked": bool(locked or raw.get("locked")),
                "status": str(raw.get("status") or "active"),
            }
            position = positions.get(key)
            if position is None:
                positions[key] = len(records)
                records.append(proposed)
                continue
            current_entry = records[position]
            if current_entry.get("locked") and not proposed["locked"]:
                if _normalized(current_entry.get("target")) != _normalized(target):
                    payload.setdefault("conflicts", []).append(
                        {
                            "source": source,
                            "kept": current_entry.get("target"),
                            "rejected": target,
                            "reason": "locked_manual_entry",
                        }
                    )
                continue
            records[position] = proposed
        payload["entries"] = records
        return self.replace(current["id"], current["revision"], payload)
