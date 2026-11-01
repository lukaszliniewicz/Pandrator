"""Current provider catalogue scope; selecting a scope does not grant access."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .credential_identity import CredentialNamespace, credential_namespace
from .job_identity import job_creation_ownership
from .models import Provider, ProviderModel

NONINITIAL_LLM_PROVIDER_KEYS = frozenset(
    {
        "openai",
        "anthropic",
        "gemini",
        "openrouter",
        "groq",
        "mistral",
        "azure",
        "vertex_ai",
    }
)


class LlmSettingsHydrator(Protocol):
    def __call__(
        self,
        settings: dict[str, Any],
        stage: str,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]: ...


def provider_namespace(
    session: Session,
    *,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> CredentialNamespace:
    """Derive an active namespace from current typed SQL roots, never settings JSON."""
    with session.no_autoflush:
        owner = job_creation_ownership(session, session_id=session_id, workspace_id=workspace_id)
        return credential_namespace(session, owner.workspace_id)


def provider_in_namespace(
    session: Session, provider_id: str, namespace: CredentialNamespace
) -> Provider | None:
    return session.scalar(
        select(Provider)
        .where(Provider.workspace_id == namespace.workspace_id, Provider.id == provider_id)
        .execution_options(populate_existing=True)
    )


def model_in_namespace(
    session: Session,
    model_record_id: str,
    namespace: CredentialNamespace,
    *,
    provider_id: str | None = None,
) -> ProviderModel | None:
    statement = (
        select(ProviderModel)
        .join(Provider, ProviderModel.provider_id == Provider.id)
        .where(
            Provider.workspace_id == namespace.workspace_id,
            ProviderModel.id == model_record_id,
        )
    )
    if provider_id is not None:
        statement = statement.where(Provider.id == provider_id)
    return session.scalar(statement.execution_options(populate_existing=True))


__all__ = [
    "LlmSettingsHydrator",
    "NONINITIAL_LLM_PROVIDER_KEYS",
    "model_in_namespace",
    "provider_in_namespace",
    "provider_namespace",
]
