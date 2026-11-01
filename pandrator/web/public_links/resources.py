"""Purpose-specific project service projection with no workspace catalogue grant."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from pandrator.logic.tts_service_identity import normalize_service_id

from ..access.contracts import AccessContext
from ..access.session_policy import require_session_access
from ..accounts.actor_authorization import require_actor_in_session
from ..credential_identity import credential_namespace
from ..models import SessionRecord
from ..stt_providers import stt_catalogue_settings_in_session, stt_catalogue_snapshot
from ..tts_catalogue_projection import TtsCatalogueServiceNotFoundError
from .contracts import LINK_SCOPES, ProjectLinkContext, PublicLinkError

if TYPE_CHECKING:
    from ..application_services import ApplicationServices

_ROW_TEXT = frozenset({"id", "name", "adapter"})
_ROW_FLAGS = frozenset(
    {
        "available",
        "online",
        "supports_voice_cloning",
        "supports_predefined_voices",
        "has_multiple_models",
    }
)
_MODEL_TEXT = frozenset({"id", "label", "family", "voice_mode"})
_MODEL_FLAGS = frozenset({"available", "online", "loaded"})
_LOCAL_STT = frozenset(
    {
        "auto",
        "automatic",
        "parakeet_preferred",
        "whisper",
        "whisperx",
        "parakeet",
        "parakeet_onnx",
        "moss",
        "qwen3",
        "qwen",
        "qwen3_asr",
        "qwen_3",
        "qwen_3_asr",
    }
)


def _denied() -> PublicLinkError:
    return PublicLinkError("link_operation_denied", 403)


def _string(value: Any) -> str:
    return value.strip() if type(value) is str else ""


def _warning(code: str, service_id: str, model_id: str = "") -> dict[str, str]:
    return {"code": code, "service_id": service_id, "model_id": model_id}


def _project_row(source: dict[str, Any], model_id: str) -> tuple[dict[str, Any], bool]:
    """Copy a closed DTO, including closed per-model records, without generic deepcopy."""
    row: dict[str, Any] = {key: value for key in _ROW_TEXT if type(value := source.get(key)) is str}
    row.update(
        {
            key: value
            for key in _ROW_FLAGS
            if key in source and (type(value := source[key]) is bool or value is None)
        }
    )
    models = source.get("models")
    known = (
        {_string(item) for item in models if type(item) is str}
        if isinstance(models, list)
        else set()
    )
    catalog = source.get("model_catalog")
    records = (
        [item for item in catalog if isinstance(item, dict)] if isinstance(catalog, list) else []
    )
    known.update(_string(item.get("id")) for item in records)
    known.update(_string(source.get(key)) for key in ("model", "default_model"))
    known.discard("")
    row["models"] = [model_id] if model_id and model_id in known else []
    row["model_catalog"] = []
    for model in records:
        if not model_id or _string(model.get("id")) != model_id:
            continue
        result: dict[str, Any] = {
            key: value for key in _MODEL_TEXT if type(value := model.get(key)) is str
        }
        result.update(
            {
                key: value
                for key in _MODEL_FLAGS
                if key in model and (type(value := model[key]) is bool or value is None)
            }
        )
        languages = model.get("supported_languages")
        if isinstance(languages, list) and all(type(language) is str for language in languages):
            result["supported_languages"] = list(languages)
        row["model_catalog"].append(result)
    return row, bool(model_id and model_id not in known)


def project_link_catalogue(
    services: ApplicationServices,
    context: ProjectLinkContext,
    session_id: str,
    kind: str,
) -> dict[str, Any]:
    """Read only the current project's selected service/model, never a voice library."""
    if (
        type(context) is not ProjectLinkContext
        or context.actor.principal_kind != "project_link"
        or context.link.get("mode") != "manage_project"
        or context.actor.scopes != LINK_SCOPES["manage_project"]
        or type(kind) is not str
        or kind not in {"tts", "stt"}
        or type(session_id) is not str
        or not session_id
    ):
        raise _denied()
    with services.database.snapshot_session() as session:
        try:
            require_actor_in_session(session, context.actor, required_scopes={"app.read"})
            access = require_session_access(
                session,
                AccessContext(context.actor),
                session_id,
                expected_project_id=context.link["project_id"],
            )
            if (
                session.scalar(
                    select(SessionRecord.id).where(
                        SessionRecord.id == session_id, SessionRecord.trashed_at.is_(None)
                    )
                )
                is None
            ):
                raise _denied()
        except (PermissionError, KeyError):
            raise _denied() from None
        resolved, _settings_hash = services.workspace_settings.resolve_in_session(
            session, session_id, sections=[kind]
        )
        effective = resolved[kind]
        selector = (
            _string(effective.get("service"))
            if kind == "tts"
            else _string(effective.get("stt_engine") or effective.get("stt_backend"))
        )
        service_id = (
            normalize_service_id(selector)
            if kind == "tts"
            else selector.lower().replace("-", "_").replace(" ", "_")
        )
        result: dict[str, Any] = {
            "services": [],
            "session_id": session_id,
            "scope": "project",
            "can_edit": False,
            "warnings": [],
        }
        if kind == "stt" and service_id in _LOCAL_STT:
            return result
        if not service_id:
            return result
        if kind == "tts":
            captured = services.tts_catalogue._settings(
                db_session=session, workspace_id=access.workspace_id
            )
            allow_host_adc = credential_namespace(
                session, workspace_id=access.workspace_id
            ).host_credentials_allowed
            try:
                payload, _revision = services.tts_catalogue.snapshot(
                    view="compact",
                    refresh=False,
                    approved_only=True,
                    service_ids=[service_id],
                    workspace_id=access.workspace_id,
                    settings_snapshot=captured,
                    allow_host_adc=allow_host_adc,
                )
            except TtsCatalogueServiceNotFoundError:
                result["warnings"].append(_warning("configured_service_unavailable", service_id))
                return result
        else:
            captured_stt = stt_catalogue_settings_in_session(
                session, workspace_id=access.workspace_id
            )
            # The STT approved path is a pure configured-record projection: it
            # has no refresh parameter, credentials or provider discovery calls.
            payload, _revision = stt_catalogue_snapshot(
                services.database,
                services.paths,
                workspace_id=access.workspace_id,
                settings_snapshot=captured_stt,
                approved_only=True,
            )
        source = next(
            (
                item
                for item in payload.get("services", [])
                if isinstance(item, dict) and normalize_service_id(item.get("id")) == service_id
            ),
            None,
        )
        if source is None:
            result["warnings"].append(_warning("configured_service_unavailable", service_id))
            return result
        model_id = _string(effective.get("model"))
        if not model_id:
            model_id = _string(source.get("model")) if kind == "stt" else ""
            model_id = model_id or _string(source.get("default_model"))
        row, missing_model = _project_row(source, model_id)
        result["services"] = [row]
        if missing_model:
            result["warnings"].append(
                _warning("configured_model_unavailable", service_id, model_id)
            )
        return result
