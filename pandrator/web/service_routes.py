"""HTTP catalogue, discovery, model lifecycle and queued speech previews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from flask import jsonify, request
from werkzeug.datastructures import MultiDict

from pandrator.logic.audio_cpp_catalogue import catalogue_page
from pandrator.logic.model_catalogue import catalogue_page as model_catalogue_page

from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .schemas import TtsEndpointDiscoveryRequest, TtsVoicePreviewRequest
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
    inspect_voice_idempotency = idempotency.inspect

    @app.get("/api/v1/services/tts")
    @require_auth
    def tts_services():
        from .tts_providers import TTS_CATALOGUE_VIEWS, TtsCatalogueServiceNotFoundError

        view = str(request.args.get("view", "full") or "full").strip().lower()
        if view not in TTS_CATALOGUE_VIEWS:
            return error_response(
                "validation_error",
                "view must be 'full' or 'compact'.",
                422,
            )
        try:
            selection = _tts_service_selection(request.args)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        try:
            payload, revision = tts_catalogue.snapshot(
                refresh=request.args.get("refresh", "").lower() in {"1", "true", "yes"},
                view=view,
                service_ids=selection,
            )
        except TtsCatalogueServiceNotFoundError as error:
            return error_response("tts_service_not_found", str(error), 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(payload)
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.get("/api/v1/services/tts/<service_id>")
    @require_auth
    def tts_service_detail(service_id: str):
        from .tts_providers import (
            TtsCatalogueModelNotFoundError,
            TtsCatalogueServiceNotFoundError,
        )

        try:
            selected_models = _tts_model_selection(request.args)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        try:
            payload, revision = tts_catalogue.service_detail(
                service_id,
                refresh=request.args.get("refresh", "").lower() in {"1", "true", "yes"},
                models=selected_models,
            )
        except TtsCatalogueServiceNotFoundError as error:
            return error_response("tts_service_not_found", str(error), 404)
        except TtsCatalogueModelNotFoundError as error:
            return error_response("tts_model_not_found", str(error), 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(payload)
        response.headers["ETag"] = f'"{revision}"'
        return response

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
    def stt_services():
        from .stt_providers import stt_catalogue_snapshot

        payload, revision = stt_catalogue_snapshot(database, paths)
        response = jsonify(payload)
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.get("/api/v1/services/tts/xtts/models")
    @require_auth
    def xtts_models_list():
        catalogue, _revision = tts_catalogue.snapshot()
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)
        try:
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
    def xtts_model_upload():
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

        catalogue, _revision = tts_catalogue.snapshot()
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)

        try:
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
    def xtts_model_delete(model_id: str):
        model_id_error = _xtts_model_id_error(model_id)
        if model_id_error:
            return error_response("validation_error", model_id_error, 422)
        catalogue, _revision = tts_catalogue.snapshot()
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)
        try:
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
    def tts_service_discover():
        from pandrator.logic import tts_handler
        from pandrator.logic.tts_endpoint_discovery import discover_tts_endpoint

        payload = TtsEndpointDiscoveryRequest.model_validate(request.get_json(silent=True) or {})
        api_key = str(payload.api_key or "").strip() or tts_catalogue.discovery_api_key(
            payload.service_id
        )
        result = discover_tts_endpoint(payload.base_url, api_key=api_key)
        result["models"] = tts_handler.normalize_tts_model_catalog(
            payload.service_id,
            result.get("models") or [],
        )
        return jsonify(result), 200 if result.get("success") else 422

    @app.post("/api/v1/services/tts/<service_id>/preview")
    @require_auth
    def tts_voice_preview(service_id: str):
        payload = TtsVoicePreviewRequest.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        idempotency_payload = {
            "service_id": service_id,
            "request": payload.model_dump(mode="json"),
        }
        replay = inspect_voice_idempotency(
            "ttsVoicePreview",
            idempotency_key,
            idempotency_payload,
        )
        if replay is not None:
            return replay
        try:
            settings = tts_catalogue.preview_settings(
                service_id,
                model=payload.model,
                voice=payload.voice,
                language=payload.language,
                generation_prompt=payload.generation_prompt,
                seed=payload.seed,
                preserve_blank_voice=(
                    "voice" in payload.model_fields_set and not payload.voice.strip()
                ),
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        if settings is None:
            return error_response("not_found", "TTS service not found.", 404)
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="ttsVoicePreview",
                            idempotency_key=idempotency_key,
                            payload=idempotency_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation)
                    if replay is not None:
                        return replay
                    job = jobs.enqueue_in_session(
                        db_session,
                        "tts.preview",
                        {"text": payload.text, "settings": settings},
                        max_attempts=2,
                        resource_keys=[f"service:tts:{service_id}"],
                    )
                    result = _job_payload(job)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            return jsonify(result), 202
        job = jobs.enqueue(
            "tts.preview",
            {"text": payload.text, "settings": settings},
            max_attempts=2,
            resource_keys=[f"service:tts:{service_id}"],
        )
        return jsonify(_job_payload(job)), 202
