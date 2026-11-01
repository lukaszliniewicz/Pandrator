"""HTTP catalogue, discovery, model lifecycle and queued speech previews."""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import wraps
from typing import Any

import requests
from flask import jsonify, request
from sqlalchemy import select
from werkzeug.datastructures import MultiDict

from pandrator.logic.audio_cpp_catalogue import catalogue_page
from pandrator.logic.model_catalogue import catalogue_page as model_catalogue_page
from pandrator.logic.tts_service_identity import normalize_service_id

from .access.contracts import ProjectNotFound, WorkspaceNotFound
from .access.job_policy import JobNotFound
from .access.session_policy import SessionNotFound
from .access.speech_preview_policy import require_preview_replay, require_preview_workspace
from .access.workspace_policy import WorkspaceAction
from .accounts.execution_actor import execution_actor_scope
from .credential_identity import credential_namespace
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import ApiIdempotency
from .route_context import RouteContext
from .schemas import TtsEndpointDiscoveryRequest, TtsVoicePreviewRequest
from .speech_catalogue_access import (
    CatalogueChanged,
    SpeechFence,
    SpeechScopeInvalid,
    require_speech_access,
    scope_identifier,
)
from .speech_catalogue_projection import (
    approved_tts_payload,
    public_configuration,
    summary_tts_payload,
)
from .voice_provider_settings import capture_voice_provider_settings, check_voice_provider_settings
from .xtts_model_proxy import (
    _XTTS_MODEL_BUNDLE_FILENAME_SET,
    _XTTS_MODEL_UPLOAD_TIMEOUT_SECONDS,
    XTTS_MODEL_BUNDLE_FILENAMES,
    _xtts_health_endpoint,
    _xtts_lifecycle_item,
    _xtts_model_bundle_upload_parts,
    _xtts_model_id_error,
    _xtts_models_endpoint,
    _xtts_service_endpoint_base,
    _xtts_wrapper_delete_unsupported,
    _xtts_wrapper_error_details,
    _xtts_wrapper_error_message,
)


def _tts_service_selection(args: Any) -> list[str] | None:
    """Parse the TTS catalogue service filter query params.

    Returns the raw selection (or None for the whole catalogue).
    ``service_id`` and ``services`` are mutually exclusive; problems raise
    ValueError for a 422 response. Unknown ids are reported by the catalogue
    service as 404, never silently dropped.
    """
    from .tts_providers import MAX_TTS_SERVICE_FILTER_IDS

    single = str(args.get("service_id", "") or "").strip()
    multiple = str(args.get("services", "") or "").strip()
    if single and multiple:
        raise ValueError("Use service_id or services, not both.")
    raw: list[str] = []
    if single:
        raw = [single]
    elif multiple:
        raw = [part.strip() for part in multiple.split(",")]
    if not raw:
        return None
    if any(not part or len(part) > 64 for part in raw):
        raise ValueError("Each TTS service id must be 1 to 64 characters.")
    if len(raw) > MAX_TTS_SERVICE_FILTER_IDS:
        raise ValueError(f"Select at most {MAX_TTS_SERVICE_FILTER_IDS} TTS services per request.")
    return raw


def _tts_model_selection(args: Any) -> list[str] | None:
    """Parse the TTS service-detail model filter query params.

    ``model`` selects one model, ``models`` a comma-separated list; both
    together is a 422. Returns None when no filter was given. Entries are
    validated for transport safety here; existence is checked against the
    service catalogue (unknown models are a 404, never silent).
    Model ids are case-sensitive provider identifiers and are not normalized.
    """
    single = str(args.get("model", "") or "").strip()
    multiple = str(args.get("models", "") or "").strip()
    if single and multiple:
        raise ValueError("Use model or models, not both.")
    if single:
        return [single]
    if not multiple:
        return None
    selected = [part.strip() for part in multiple.split(",")]
    if any(not part or len(part) > 256 for part in selected):
        raise ValueError("Each selected TTS model id must be 1 to 256 characters.")
    return selected


@dataclass(frozen=True, slots=True)
class _CatalogueQuery:
    strings: dict[str, str]
    commercial_use: str
    recommended_only: bool
    limit: int
    offset: int


def _catalogue_query(
    args: MultiDict[str, str], *, include_provider: bool = False
) -> _CatalogueQuery:
    names = ("category", "family", "query", "language", "capability")
    if include_provider:
        names = (*names, "provider")
    string_filters = {name: args.get(name, "") for name in names}
    for name, value in string_filters.items():
        if len(value) > 160:
            raise ValueError(f"{name} must be at most 160 characters.")

    commercial_use = args.get("commercial_use", "")
    if commercial_use not in {
        "",
        "permitted",
        "noncommercial",
        "conditional",
        "unknown",
    }:
        raise ValueError(
            "commercial_use must be one of: permitted, noncommercial, conditional, unknown."
        )

    recommended_only = args.get("recommended_only", "false").strip().lower()
    if recommended_only not in {"true", "false"}:
        raise ValueError("recommended_only must be true or false.")

    try:
        raw_limit = args.get("limit")
        limit = int(raw_limit) if raw_limit is not None else 30
        raw_offset = args.get("offset")
        offset = int(raw_offset) if raw_offset is not None else 0
        if not 1 <= limit <= 100:
            raise ValueError
        if not 0 <= offset <= 10_000:
            raise ValueError
    except (TypeError, ValueError) as error:
        raise ValueError(
            "limit must be an integer from 1 through 100 and offset must be an integer from 0 through 10000."
        ) from error

    return _CatalogueQuery(
        string_filters, commercial_use, recommended_only == "true", limit, offset
    )


def register_service_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    idempotency: MutationIdempotency,
) -> None:
    services = context.services
    database = services.database
    paths = services.paths
    tts_catalogue = services.tts_catalogue
    jobs = services.jobs
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    replay_idempotency = idempotency.replay

    def speech_errors(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except CatalogueChanged as error:
                return error_response("catalogue_changed", str(error), 409)
            except (WorkspaceNotFound, ProjectNotFound, SessionNotFound):
                return error_response("not_found", "Speech workspace not found.", 404)
            except SpeechScopeInvalid as error:
                return error_response("validation_error", str(error), 422)

        return wrapped

    def passive_refresh_error():
        if request.args.get("refresh", "").strip().lower() in {"1", "true", "yes"}:
            return error_response(
                "catalogue_refresh_requires_post", "Use POST to refresh the speech catalogue.", 409
            )
        return None

    def capture_tts(
        *, action=WorkspaceAction.READ, refresh=False, admin=False, revisions=False, sessions=True
    ):
        if not sessions and "session_id" in request.args:
            raise SpeechScopeInvalid("Session scope is only available for catalogue reads.")
        actual_context = context.guards.access_context()
        with database.snapshot_session() as db_session:
            access = require_speech_access(
                db_session,
                actual_context,
                request.args.get("workspace_id"),
                session_id=request.args.get("session_id") if sessions else None,
                action=action,
                refresh=refresh,
                admin=admin,
            )
            settings = tts_catalogue._settings(
                db_session=db_session, workspace_id=access.workspace.id
            )
            allow_host_adc = credential_namespace(
                db_session, workspace_id=access.workspace.id
            ).host_credentials_allowed
        original_action = (
            action
            if action is not WorkspaceAction.READ
            else WorkspaceAction.MANAGE_READ
            if access.configuration
            else WorkspaceAction.READ
        )
        fence = SpeechFence(
            database,
            actual_context,
            access.workspace.id,
            original_action,
            refresh=refresh,
            admin=admin or access.legacy_manager,
            revisions=(settings[1], settings[3]) if revisions else None,
            expected_host_credentials=allow_host_adc,
        )
        return access, settings, fence, allow_host_adc

    def finish_catalogue(payload, revision, access, fence):
        fence()
        if access.configuration and not access.legacy_manager:
            payload = public_configuration(payload)
        payload.update(access.metadata())
        response = jsonify(payload)
        response.headers["ETag"] = f'"{revision}"'
        return response

    def catalogue_response(service_id=None, *, refresh=False):
        from .tts_providers import (
            TTS_CATALOGUE_VIEWS,
            TtsCatalogueModelNotFoundError,
            TtsCatalogueServiceNotFoundError,
        )

        error = passive_refresh_error() if not refresh else None
        if error is not None:
            return error
        try:
            view = str(request.args.get("view", "full") or "full").strip().lower()
            summary = view == "summary"
            if summary and service_id is not None:
                raise ValueError("Summary view is only available for TTS collections.")
            if view not in (*TTS_CATALOGUE_VIEWS, "summary"):
                raise ValueError("view must be 'full', 'compact' or 'summary'.")
            selection = _tts_service_selection(request.args) if service_id is None else None
            models = _tts_model_selection(request.args) if service_id is not None else None
            access, settings, fence, allow_host_adc = capture_tts(
                action=WorkspaceAction.MANAGE if refresh else WorkspaceAction.READ,
                refresh=refresh,
                revisions=refresh,
                sessions=not refresh,
            )
            options = {
                "refresh": refresh,
                "workspace_id": access.workspace.id,
                "settings_snapshot": settings,
                "allow_host_adc": allow_host_adc,
                "approved_only": not refresh if summary else not access.configuration,
                "before_io": fence if refresh else None,
            }
            if service_id is None:
                payload, revision = tts_catalogue.snapshot(
                    view="compact" if summary else view, service_ids=selection, **options
                )
                if summary:
                    if refresh:
                        observed = {}
                        for service in payload.get("services", []):
                            state: dict[str, Any] = {
                                key: value
                                for key, value in service.items()
                                if (key in {"online", "available"} and type(value) is bool)
                                or (key == "health_checked_at" and isinstance(value, str))
                            }
                            catalogue = service.get("catalogue")
                            if isinstance(catalogue, dict):
                                state["catalogue"] = {
                                    key: value
                                    for key, value in catalogue.items()
                                    if (key == "complete" and type(value) is bool)
                                    or (
                                        key in {"source", "scope", "service_id", "checked_at"}
                                        and (value is None or isinstance(value, str))
                                    )
                                }
                            observed[service["id"]] = state
                        payload = approved_tts_payload(payload)
                        for service in payload.get("services", []):
                            service.update(observed.get(service.get("id"), {}))
                    payload = summary_tts_payload(payload)
            else:
                payload, revision = tts_catalogue.service_detail(
                    service_id, models=models, allow_refresh_fallback=False, **options
                )
            return finish_catalogue(payload, revision, access, fence)
        except TtsCatalogueServiceNotFoundError as error:
            return error_response("tts_service_not_found", str(error), 404)
        except TtsCatalogueModelNotFoundError as error:
            return error_response("tts_model_not_found", str(error), 404)
        except CatalogueChanged:
            raise
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    def live_scope(action):
        def decorate(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                captured = capture_tts(action=action, admin=True, sessions=False)
                response = function(*args, _speech=captured, **kwargs)
                captured[2]()
                return response

            return wrapped

        return decorate

    @app.get("/api/v1/services/tts")
    @require_auth
    @speech_errors
    def tts_services():
        return catalogue_response()

    @app.get("/api/v1/services/tts/<service_id>")
    @require_auth
    @speech_errors
    def tts_service_detail(service_id: str):
        return catalogue_response(service_id)

    @app.post("/api/v1/services/tts/refresh")
    @require_auth
    @speech_errors
    def tts_services_refresh():
        return catalogue_response(refresh=True)

    @app.post("/api/v1/services/tts/<service_id>/refresh")
    @require_auth
    @speech_errors
    def tts_service_refresh(service_id: str):
        return catalogue_response(service_id, refresh=True)

    @app.get("/api/v1/services/audio-cpp/catalogue")
    @require_auth
    def audio_cpp_catalogue():
        try:
            filters = _catalogue_query(request.args)
            payload = catalogue_page(
                **filters.strings,
                commercial_use=filters.commercial_use,
                recommended_only=filters.recommended_only,
                limit=filters.limit,
                offset=filters.offset,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(payload)

    @app.get("/api/v1/services/models/catalogue")
    @require_auth
    def model_catalogue():
        try:
            filters = _catalogue_query(request.args, include_provider=True)
            payload = model_catalogue_page(
                **filters.strings,
                commercial_use=filters.commercial_use,
                recommended_only=filters.recommended_only,
                limit=filters.limit,
                offset=filters.offset,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(payload)

    @app.get("/api/v1/services/stt")
    @require_auth
    @speech_errors
    def stt_services():
        from .stt_providers import stt_catalogue_settings_in_session, stt_catalogue_snapshot

        error = passive_refresh_error()
        if error is not None:
            return error
        actual_context = context.guards.access_context()
        with database.snapshot_session() as db_session:
            access = require_speech_access(
                db_session,
                actual_context,
                request.args.get("workspace_id"),
                session_id=request.args.get("session_id"),
            )
            settings = stt_catalogue_settings_in_session(
                db_session, workspace_id=access.workspace.id
            )
        fence = SpeechFence(
            database,
            actual_context,
            access.workspace.id,
            WorkspaceAction.MANAGE_READ if access.configuration else WorkspaceAction.READ,
        )
        payload, revision = stt_catalogue_snapshot(
            database,
            paths,
            workspace_id=access.workspace.id,
            settings_snapshot=settings,
            approved_only=not access.configuration,
        )
        return finish_catalogue(payload, revision, access, fence)

    @app.get("/api/v1/services/tts/xtts/models")
    @require_auth
    @speech_errors
    @live_scope(WorkspaceAction.MANAGE_READ)
    def xtts_models_list(*, _speech):
        access, settings, fence, allow_host_adc = _speech
        catalogue, _revision = tts_catalogue.snapshot(
            workspace_id=access.workspace.id,
            settings_snapshot=settings,
            allow_host_adc=allow_host_adc,
            before_io=fence,
        )
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)
        try:
            fence()
            wrapper_response = requests.get(
                _xtts_models_endpoint(endpoint_base),
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer sk-placeholder",
                },
                timeout=(5, 20),
            )
        except requests.Timeout:
            return error_response(
                "xtts_model_list_timeout",
                "The XTTS model service did not respond while listing models.",
                504,
            )
        except requests.ConnectionError:
            return error_response(
                "xtts_service_unavailable",
                "Could not connect to the selected XTTS service.",
                503,
            )
        except requests.RequestException:
            return error_response(
                "xtts_model_list_failed",
                "The XTTS model service could not list models.",
                502,
            )
        if wrapper_response.status_code >= 400:
            return error_response(
                "xtts_model_list_rejected",
                _xtts_wrapper_error_message(wrapper_response),
                wrapper_response.status_code if wrapper_response.status_code < 500 else 502,
                _xtts_wrapper_error_details(wrapper_response),
            )
        try:
            wrapper_payload = wrapper_response.json()
            wrapper_items = (
                wrapper_payload.get("data") if isinstance(wrapper_payload, dict) else None
            )
        except ValueError:
            wrapper_items = None
        if not isinstance(wrapper_items, list):
            return error_response(
                "xtts_model_list_failed",
                "The XTTS model service returned an unexpected model list.",
                502,
            )
        items = [
            normalized
            for item in wrapper_items
            if (normalized := _xtts_lifecycle_item(item)) is not None
        ]
        lifecycle_supported = bool(items) and all(item["lifecycle_supported"] for item in items)
        wrapper: dict[str, str] | None = None
        try:
            fence()
            health_response = requests.get(
                _xtts_health_endpoint(endpoint_base),
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer sk-placeholder",
                },
                timeout=(2, 5),
            )
            health_payload = health_response.json()
            if isinstance(health_payload, dict):
                version = str(health_payload.get("version") or "").strip()
                status = str(health_payload.get("status") or "").strip()
                if version or status:
                    wrapper = {
                        key: value
                        for key, value in {"version": version, "status": status}.items()
                        if value
                    }
        except (requests.RequestException, ValueError):
            # Listing remains useful when old wrappers do not expose health.
            pass
        return jsonify(
            {
                "object": "list",
                "data": items,
                "lifecycle_supported": lifecycle_supported,
                "compatibility": (
                    None
                    if lifecycle_supported
                    else "This XTTS component can list and use models, but cannot safely remove them. Update or Repair XTTS in Pandrator Manager to enable lifecycle management."
                ),
                "wrapper": wrapper,
            }
        )

    @app.post("/api/v1/services/tts/xtts/models")
    @require_auth
    @speech_errors
    @live_scope(WorkspaceAction.MANAGE)
    def xtts_model_upload(*, _speech):
        if set(request.form.keys()) != {"model_id"}:
            return error_response(
                "validation_error",
                "Upload exactly one model_id field with the XTTS bundle.",
                422,
            )
        model_ids = [str(value or "").strip() for value in request.form.getlist("model_id")]
        model_id_error = _xtts_model_id_error(model_ids[0]) if len(model_ids) == 1 else ""
        if len(model_ids) != 1 or model_id_error:
            return error_response(
                "validation_error",
                model_id_error or "Upload exactly one model_id field with the XTTS bundle.",
                422,
            )
        if set(request.files.keys()) != {"files"}:
            return error_response(
                "validation_error",
                "Upload the XTTS bundle as repeated 'files' fields.",
                422,
            )
        uploaded_files = request.files.getlist("files")
        filenames = [str(uploaded_file.filename or "") for uploaded_file in uploaded_files]
        if (
            len(uploaded_files) != len(XTTS_MODEL_BUNDLE_FILENAMES)
            or set(filenames) != _XTTS_MODEL_BUNDLE_FILENAME_SET
        ):
            return error_response(
                "validation_error",
                "XTTS model bundles must contain exactly config.json, model.pth, speakers_xtts.pth, and vocab.json. Nested folders and incomplete training outputs are not supported.",
                422,
            )
        try:
            boundary, body = _xtts_model_bundle_upload_parts(
                model_ids[0],
                uploaded_files,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

        access, settings, fence, allow_host_adc = _speech
        catalogue, _revision = tts_catalogue.snapshot(
            workspace_id=access.workspace.id,
            settings_snapshot=settings,
            allow_host_adc=allow_host_adc,
            before_io=fence,
        )
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)

        try:
            fence()
            wrapper_response = requests.post(
                _xtts_models_endpoint(endpoint_base),
                data=body,
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer sk-placeholder",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                timeout=(10, _XTTS_MODEL_UPLOAD_TIMEOUT_SECONDS),
            )
        except requests.Timeout:
            return error_response(
                "xtts_model_upload_timeout",
                "The XTTS model service did not finish the upload before the one-hour timeout.",
                504,
            )
        except requests.ConnectionError:
            return error_response(
                "xtts_service_unavailable",
                "Could not connect to the selected XTTS service.",
                503,
            )
        except requests.RequestException:
            return error_response(
                "xtts_model_upload_failed",
                "The XTTS model service could not accept the upload.",
                502,
            )

        if wrapper_response.status_code >= 400:
            if wrapper_response.status_code in {404, 405}:
                return error_response(
                    "xtts_model_upload_unsupported",
                    "This XTTS component does not support model uploads. Update the XTTS component in Pandrator Manager, then retry.",
                    wrapper_response.status_code,
                )
            if 400 <= wrapper_response.status_code < 500:
                return error_response(
                    "xtts_model_upload_rejected",
                    _xtts_wrapper_error_message(wrapper_response),
                    wrapper_response.status_code,
                )
            return error_response(
                "xtts_model_upload_failed",
                "The XTTS model service failed while installing the model.",
                502,
            )
        try:
            wrapper_payload = wrapper_response.json()
        except ValueError:
            wrapper_payload = None
        if (
            wrapper_response.status_code != 201
            or not isinstance(wrapper_payload, dict)
            or not isinstance(wrapper_payload.get("id"), str)
            or not str(wrapper_payload["id"]).strip()
            or not isinstance(wrapper_payload.get("object"), str)
            or not isinstance(wrapper_payload.get("owned_by"), str)
            or isinstance(wrapper_payload.get("bytes"), bool)
            or not isinstance(wrapper_payload.get("bytes"), int)
        ):
            return error_response(
                "xtts_model_upload_failed",
                "The XTTS model service returned an unexpected upload response.",
                502,
            )
        return (
            jsonify(
                {
                    "id": str(wrapper_payload["id"]),
                    "object": str(wrapper_payload["object"]),
                    "owned_by": str(wrapper_payload["owned_by"]),
                    "bytes": int(wrapper_payload["bytes"]),
                    **(
                        {
                            key: wrapper_payload.get(key)
                            for key in (
                                "created",
                                "is_default",
                                "is_local",
                                "removable",
                                "source",
                                "relative_path",
                                "bundle_complete",
                            )
                            if key in wrapper_payload
                        }
                    ),
                }
            ),
            201,
        )

    @app.delete("/api/v1/services/tts/xtts/models/<path:model_id>")
    @require_auth
    @speech_errors
    @live_scope(WorkspaceAction.MANAGE)
    def xtts_model_delete(model_id: str, *, _speech):
        model_id_error = _xtts_model_id_error(model_id)
        if model_id_error:
            return error_response("validation_error", model_id_error, 422)
        access, settings, fence, allow_host_adc = _speech
        catalogue, _revision = tts_catalogue.snapshot(
            workspace_id=access.workspace.id,
            settings_snapshot=settings,
            allow_host_adc=allow_host_adc,
            before_io=fence,
        )
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)
        try:
            fence()
            wrapper_response = requests.delete(
                _xtts_models_endpoint(endpoint_base, model_id),
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer sk-placeholder",
                },
                timeout=(10, 120),
            )
        except requests.Timeout:
            return error_response(
                "xtts_model_delete_timeout",
                "The XTTS model service did not finish removing the model.",
                504,
            )
        except requests.ConnectionError:
            return error_response(
                "xtts_service_unavailable",
                "Could not connect to the selected XTTS service.",
                503,
            )
        except requests.RequestException:
            return error_response(
                "xtts_model_delete_failed",
                "The XTTS model service could not remove the model.",
                502,
            )
        if wrapper_response.status_code >= 400:
            if _xtts_wrapper_delete_unsupported(wrapper_response):
                return error_response(
                    "xtts_model_delete_unsupported",
                    "This XTTS component cannot safely remove models. Update or Repair XTTS in Pandrator Manager, then retry.",
                    wrapper_response.status_code,
                    _xtts_wrapper_error_details(wrapper_response),
                )
            return error_response(
                "xtts_model_delete_rejected",
                _xtts_wrapper_error_message(wrapper_response),
                wrapper_response.status_code if wrapper_response.status_code < 500 else 502,
                _xtts_wrapper_error_details(wrapper_response),
            )
        try:
            wrapper_payload = wrapper_response.json()
        except ValueError:
            wrapper_payload = None
        if (
            not isinstance(wrapper_payload, dict)
            or wrapper_payload.get("id") != model_id
            or wrapper_payload.get("deleted") is not True
        ):
            return error_response(
                "xtts_model_delete_failed",
                "The XTTS model service returned an unexpected removal response.",
                502,
            )
        return jsonify(
            {
                "id": model_id,
                "object": str(wrapper_payload.get("object") or "model"),
                "deleted": True,
                "evicted": bool(wrapper_payload.get("evicted")),
            }
        )

    @app.post("/api/v1/services/tts/discover")
    @require_auth
    @speech_errors
    def tts_service_discover():
        from pandrator.logic import tts_handler
        from pandrator.logic.tts_endpoint_discovery import discover_tts_endpoint

        from .credentials import resolve_provider_credential
        from .tts_providers import TtsCatalogueServiceNotFoundError

        access, settings, fence, _allow_host_adc = capture_tts(
            action=WorkspaceAction.MANAGE, refresh=True, admin=True, sessions=False
        )
        payload = TtsEndpointDiscoveryRequest.model_validate(request.get_json(silent=True) or {})
        api_key = str(payload.api_key or "").strip()
        if not api_key and payload.service_id:
            fence = replace(fence, revisions=(settings[1], settings[3]))
            fence()
            try:
                rows, _manager = tts_catalogue._base_services(
                    settings[0],
                    settings[2],
                    selected=[normalize_service_id(payload.service_id)],
                    before_io=fence,
                )
            except TtsCatalogueServiceNotFoundError:
                return error_response("tts_service_not_found", "TTS service not found.", 404)
            saved = rows[0]
            if str(saved.get("api_base") or "").rstrip("/") != str(payload.base_url).rstrip("/"):
                return error_response(
                    "validation_error",
                    "Saved credentials require the configured service endpoint.",
                    422,
                )
            fence()
            service_id, key_env, reference = tts_catalogue._credential_details(saved)
            credential = resolve_provider_credential(
                database,
                paths,
                service_id,
                reference,
                fallback_environment_variable=key_env,
                workspace_id=access.workspace.id,
            )
            api_key = credential.resolved_value()
        fence()
        result = discover_tts_endpoint(payload.base_url, api_key=api_key)
        result["models"] = tts_handler.normalize_tts_model_catalog(
            payload.service_id, result.get("models") or []
        )
        fence()
        return jsonify(result), 200 if result.get("success") else 422

    @app.post("/api/v1/services/tts/<service_id>/preview")
    @require_auth
    @speech_errors
    def tts_voice_preview(service_id: str):
        from .speech_input_contracts import SpeechInputOwner
        from .speech_inputs import (
            capture_speech_input,
            cleanup_unpublished_speech_input,
            prepare_speech_input,
            publish_speech_input,
        )

        if "session_id" in request.args:
            return error_response("validation_error", "Session scope is not available for this preview.", 422)
        try:
            preview_workspace = scope_identifier(request.args.get("workspace_id"))
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        payload = TtsVoicePreviewRequest.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        idempotency_payload = {
            "service_id": service_id,
            # Existing receipts predate the optional managed selector.
            "request": payload.model_dump(
                mode="json", exclude={"voice_id"} if payload.voice_id is None else set()
            ),
        }
        access = context.guards.access_context()

        def scoped_principal(db_session, workspace_id):
            if idempotency_key is None:
                return None
            principal = idempotency.principal()
            if not credential_namespace(db_session, workspace_id).legacy_host_access:
                principal = replace(
                    principal,
                    subject=f"workspace:{workspace_id}:user:{access.actor.user_id}",
                )
            return principal

        def checked_replay(db_session, workspace_id, reservation):
            if reservation.record.resource_kind != "job" or not reservation.record.resource_id:
                raise JobNotFound()
            require_preview_replay(
                db_session, access, reservation.record.resource_id, workspace_id=workspace_id
            )
            saved = reservation.response
            if saved is not None:
                body, _status = saved
                legacy = credential_namespace(db_session, workspace_id).legacy_host_access
                if (not legacy and body.get("workspace_id") != workspace_id) or (
                    "workspace_id" in body and body["workspace_id"] != workspace_id
                ):
                    raise JobNotFound()
            return reservation

        def existing_replay(db_session, workspace_id, principal):
            if principal is None:
                return None
            # Prove the saved resource before digest/expiry/response disclosure.
            existing = db_session.execute(
                select(ApiIdempotency.resource_kind, ApiIdempotency.resource_id).where(
                    ApiIdempotency.principal_subject == principal.subject,
                    ApiIdempotency.operation_id == "ttsVoicePreview",
                    ApiIdempotency.idempotency_key == idempotency_key,
                )
            ).one_or_none()
            if existing is None:
                return None
            if existing.resource_kind != "job" or not existing.resource_id:
                raise JobNotFound()
            require_preview_replay(
                db_session, access, existing.resource_id, workspace_id=workspace_id
            )
            reservation = services.idempotency.replay_if_present(
                db_session, principal=principal, operation_id="ttsVoicePreview",
                idempotency_key=idempotency_key, payload=idempotency_payload,
            )
            return checked_replay(db_session, workspace_id, reservation) if reservation is not None else None

        def enqueue(db_session, workspace_id, principal, settings, revisions, speech_input):
            reservation = None
            if principal is not None:
                reservation = services.idempotency.begin(
                    db_session, principal=principal, operation_id="ttsVoicePreview",
                    idempotency_key=idempotency_key, payload=idempotency_payload,
                )
                if reservation.replayed:
                    return None, checked_replay(db_session, workspace_id, reservation)
            resource_keys = [
                f"service:tts:{normalize_service_id(settings.get('preview_service_id') or service_id)}"
            ]
            with execution_actor_scope(access.actor):
                job = jobs.enqueue_in_session(
                    db_session, "tts.preview",
                    {"text": payload.text, "settings": settings, "workspace_id": workspace_id,
                     "speech_input": speech_input, **revisions},
                    workspace_id=workspace_id, max_attempts=1, resource_keys=resource_keys,
                )
            result = _job_payload(job)
            result["workspace_id"] = workspace_id
            result["preview"] = {"directions": {
                "source": "inherited" if payload.generation_prompt is None else "custom" if payload.generation_prompt.strip() else "cleared",
                "present": bool(str(settings.get("generation_prompt") or "").strip()
                                or str(settings.get("openai_audio_instructions") or "").strip()),
            }}
            if reservation is not None:
                services.idempotency.complete(
                    db_session, reservation, response=result, status_code=202,
                    resource_kind="job", resource_id=job.id,
                )
            return result, None

        prepared = None
        adopted = False
        try:
            with database.immediate_session() as db_session:
                workspace_id = require_preview_workspace(db_session, access, preview_workspace)
                principal = scoped_principal(db_session, workspace_id)
                replay = existing_replay(db_session, workspace_id, principal)
                if replay is not None:
                    return replay_idempotency(replay)
                settings = tts_catalogue.preview_settings(
                    service_id, model=payload.model, voice=payload.voice, language=payload.language,
                    generation_prompt=payload.generation_prompt, seed=payload.seed,
                    preserve_blank_voice=("voice" in payload.model_fields_set and not payload.voice.strip()),
                    workspace_id=workspace_id, db_session=db_session,
                )
                if settings is None:
                    return error_response("not_found", "TTS service not found.", 404)
                revisions = capture_voice_provider_settings(db_session, workspace_id)
                capture = capture_speech_input(
                    db_session, paths, settings, owner=SpeechInputOwner(workspace_id), voice_id=payload.voice_id,
                )
                if not capture.requires_files:
                    result, replay = enqueue(
                        db_session, workspace_id, principal, capture.settings, revisions, None,
                    )
                    if replay is not None:
                        return replay_idempotency(replay)
                    return jsonify(result), 202

            def before_io():
                with database.snapshot_session() as db_session:
                    current_workspace = require_preview_workspace(db_session, access, preview_workspace)
                    if current_workspace != workspace_id:
                        raise WorkspaceNotFound()
                    check_voice_provider_settings(db_session, revisions, workspace_id)

            prepared = prepare_speech_input(database, paths, capture, before_io=before_io)
            with database.immediate_session() as db_session:
                current_workspace = require_preview_workspace(db_session, access, preview_workspace)
                if current_workspace != workspace_id:
                    raise WorkspaceNotFound()
                current_principal = scoped_principal(db_session, workspace_id)
                if principal != current_principal:
                    raise ValueError("Workspace credentials changed. Submit the audition again.")
                replay = existing_replay(db_session, workspace_id, current_principal)
                if replay is not None:
                    return replay_idempotency(replay)
                check_voice_provider_settings(db_session, revisions, workspace_id)
                # Source/file identity is rechecked by this persistence leaf;
                # protected byte reads/copies have already finished outside it.
                speech_input = publish_speech_input(db_session, paths, capture, prepared)
                result, replay = enqueue(
                    db_session, workspace_id, current_principal, capture.settings, revisions, speech_input,
                )
                if replay is not None:
                    raise RuntimeError("The audition receipt changed inside its writer.")
            adopted = True
            return jsonify(result), 202
        except KeyError:
            return error_response("not_found", "Speech workspace or work not found.", 404)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        finally:
            if prepared is not None and not adopted:
                cleanup_unpublished_speech_input(database, prepared)
