"""Explicit, inspectable registration for durable job handlers."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import wraps
from typing import Any

from .jobs import JobHandler


@dataclass(frozen=True, slots=True)
class JobPayloadContract:
    """Minimal durable payload shape owned by a handler domain."""

    required_fields: tuple[str, ...] = ()

    def validate(
        self,
        kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise TypeError(f"Job '{kind}' payload must be a JSON object.")
        missing = [
            field
            for field in self.required_fields
            if field not in payload or payload[field] is None or payload[field] == ""
        ]
        if missing:
            fields = ", ".join(missing)
            raise ValueError(
                f"Job '{kind}' payload is missing required field(s): {fields}."
            )
        return payload


@dataclass(frozen=True, slots=True)
class JobHandlerRegistration:
    kind: str
    domain: str
    handler: JobHandler
    payload_contract: JobPayloadContract


class JobHandlerRegistry(Mapping[str, JobHandler]):
    """A duplicate-safe registry with domain ownership metadata."""

    def __init__(self) -> None:
        self._registrations: dict[str, JobHandlerRegistration] = {}

    def register(
        self,
        kind: str,
        handler: JobHandler,
        *,
        domain: str,
        payload_contract: JobPayloadContract | None = None,
    ) -> None:
        normalized_kind = str(kind or "").strip()
        normalized_domain = str(domain or "").strip()
        if not normalized_kind:
            raise ValueError("Job kind must not be empty.")
        if not normalized_domain:
            raise ValueError(f"Job handler '{normalized_kind}' needs a domain.")
        if normalized_kind in self._registrations:
            owner = self._registrations[normalized_kind].domain
            raise ValueError(
                f"Job handler '{normalized_kind}' is already registered by '{owner}'."
            )
        contract = payload_contract or JobPayloadContract()

        @wraps(handler)
        def validated_handler(payload, progress, cancel_event):
            return handler(
                contract.validate(normalized_kind, payload),
                progress,
                cancel_event,
            )

        self._registrations[normalized_kind] = JobHandlerRegistration(
            kind=normalized_kind,
            domain=normalized_domain,
            handler=validated_handler,
            payload_contract=contract,
        )

    def register_many(
        self,
        domain: str,
        handlers: Mapping[str, JobHandler],
        *,
        payload_contracts: Mapping[str, JobPayloadContract] | None = None,
    ) -> None:
        contracts = payload_contracts or {}
        for kind, handler in handlers.items():
            self.register(
                kind,
                handler,
                domain=domain,
                payload_contract=contracts.get(kind),
            )

    def registrations(
        self,
        *,
        domain: str | None = None,
    ) -> tuple[JobHandlerRegistration, ...]:
        registrations = tuple(self._registrations.values())
        if domain is None:
            return registrations
        return tuple(item for item in registrations if item.domain == domain)

    def as_dict(self) -> dict[str, JobHandler]:
        return dict(self.items())

    def __getitem__(self, kind: str) -> JobHandler:
        return self._registrations[kind].handler

    def __iter__(self) -> Iterator[str]:
        return iter(self._registrations)

    def __len__(self) -> int:
        return len(self._registrations)
