"""Provider, model and credential administration HTTP routes."""

from __future__ import annotations

import json
from typing import Any

from flask import jsonify, request
from sqlalchemy import select

from pandrator.runtime import DataPaths

from .credentials import (
    AUXILIARY_CREDENTIALS,
    DEFAULT_PROVIDER_ENVS,
    auxiliary_credential_key,
    auxiliary_profiles,
    auxiliary_reference_map,
    configure_credential_reference,
    credential_backend,
    credential_backend_profiles,
    credential_reference_input,
    database_reference,
    delete_managed_reference,
    llm_provider_credential_key,
    provider_credential_key,
    provider_credential_status,
    redact_inline_secrets,
    resolve_provider_credential,
    set_auxiliary_reference,
    validate_provider_options,
    validate_vertex_service_account_json,
)
from .database import Database
from .domain_blueprints import DomainBlueprints
from .http_serialization import model_payload as _model_dict
from .models import Provider, ProviderModel, utcnow
from .route_context import RouteContext
from .schemas import (
    CredentialUpdate,
    ModelCreate,
    ModelUpdate,
    ProviderCreate,
    ProviderTestRequest,
    ProviderUpdate,
)


def _provider_model_payload(record: ProviderModel) -> dict[str, Any]:
    payload = _model_dict(
        record,
        (
            "id",
            "provider_id",
            "model_id",
            "is_active",
            "is_default",
            "default_temperature",
            "default_reasoning_effort",
            "input_cost_per_million",
            "cached_input_cost_per_million",
            "output_cost_per_million",
            "context_window_tokens",
            "max_output_tokens",
            "options_json",
            "revision",
        ),
    )
    input_modalities = list(record.input_modalities_json or ["text"])
    output_modalities = list(record.output_modalities_json or ["text"])
    payload["input_modalities"] = input_modalities
    payload["output_modalities"] = output_modalities
    payload["supports_audio_input"] = "audio" in input_modalities
    return payload


def _provider_payload(provider: Provider, database: Database, paths: DataPaths) -> dict[str, Any]:
    payload = _model_dict(
        provider,
        (
            "id",
            "kind",
            "provider_key",
            "label",
            "enabled",
            "base_url",
            "secret_ref",
            "options_json",
            "revision",
        ),
    )
    fallback_env = str(
        (provider.options_json or {}).get("api_key_env")
        or DEFAULT_PROVIDER_ENVS.get(provider.provider_key.lower(), "")
    )
    profile_id = str((provider.options_json or {}).get("profile_id") or "").strip().lower()
    share_credential = not bool(
        (provider.options_json or {}).get("is_custom")
        or profile_id in {"custom-openai", "lm-studio", "ollama"}
    )
    payload.update(
        provider_credential_status(
            database,
            paths,
            provider.provider_key,
            provider.secret_ref,
            fallback_environment_variable=fallback_env,
            shared=share_credential,
        )
    )
    payload["credential_backend"] = credential_backend(provider.secret_ref)
    payload["credential_reference"] = credential_reference_input(provider.secret_ref)
    payload["options_json"] = redact_inline_secrets(payload.get("options_json") or {})
    return payload


def register_provider_routes(app: DomainBlueprints, context: RouteContext) -> None:
    database = context.services.database
    paths = context.services.paths
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth

    @app.get("/api/v1/providers")
    @require_auth
    def provider_list():
        with database.session() as db_session:
            providers = list(db_session.scalars(select(Provider).order_by(Provider.label)).all())
            return jsonify(
                {"items": [_provider_payload(item, database, paths) for item in providers]}
            )

    @app.get("/api/v1/credential-backends")
    @require_auth
    def credential_backends():
        return jsonify({"items": credential_backend_profiles()})

    @app.get("/api/v1/providers/profiles")
    @require_auth
    def provider_profiles():
        from .provider_settings import list_llm_provider_profiles

        return jsonify({"items": list_llm_provider_profiles()})

    @app.post("/api/v1/providers")
    @require_auth
    def provider_create():
        payload = ProviderCreate.model_validate(request.get_json(silent=True) or {})
        try:
            validate_provider_options(payload.options)
            if (
                payload.provider_key.strip().lower() == "vertex_ai"
                and str(payload.api_key or "").strip()
            ):
                validate_vertex_service_account_json(payload.api_key)
            if payload.credential_backend is not None and payload.secret_ref:
                raise ValueError(
                    "Use the structured credential storage fields or a legacy secret_ref, not both."
                )
            if payload.secret_ref and credential_backend(payload.secret_ref) == "unavailable":
                raise ValueError("The legacy secret_ref uses an unsupported credential scheme.")
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        try:
            with database.session() as db_session:
                provider = Provider(
                    kind=payload.kind,
                    provider_key=payload.provider_key,
                    label=payload.label,
                    enabled=payload.enabled,
                    base_url=payload.base_url,
                    secret_ref=payload.secret_ref,
                    options_json=payload.options,
                )
                db_session.add(provider)
                db_session.flush()
                if payload.credential_backend is not None or str(payload.api_key or "").strip():
                    key = llm_provider_credential_key(
                        provider.provider_key, provider.id, provider.options_json
                    )
                    credential_kind = (
                        "credentials"
                        if provider.provider_key.strip().lower() == "vertex_ai"
                        else "API key"
                    )
                    configured = configure_credential_reference(
                        db_session,
                        database,
                        paths,
                        key=key,
                        label=f"{provider.label} {credential_kind}",
                        current_reference="",
                        backend=payload.credential_backend or "database",
                        locator=payload.credential_reference or "",
                        secret_value=payload.api_key or "",
                    )
                    provider.secret_ref = configured.reference
                db_session.flush()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            return error_response("credential_unavailable", str(error), 422)
        result = _provider_payload(provider, database, paths)
        return jsonify(result), 201

    @app.patch("/api/v1/providers/<provider_id>")
    @require_auth
    def provider_update(provider_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current provider revision.",
                428,
            )
        payload = ProviderUpdate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            if provider.revision != expected_revision:
                return error_response(
                    "revision_conflict", "The provider changed in another client.", 409
                )
            previous_secret_ref = str(provider.secret_ref or "")
            changes = payload.model_dump(exclude_unset=True)
            submitted_key = str(changes.pop("api_key", "") or "").strip()
            clear_key = bool(changes.pop("clear_api_key", False))
            requested_backend = changes.pop("credential_backend", None)
            requested_locator = changes.pop("credential_reference", "")
            delete_previous = bool(changes.pop("delete_previous_credential", False))
            if submitted_key and clear_key:
                return error_response(
                    "validation_error",
                    "Choose either a replacement API key or remove the current key.",
                    422,
                )
            if requested_backend is not None and "secret_ref" in changes:
                return error_response(
                    "validation_error",
                    "Use the structured credential storage fields or a legacy secret_ref, not both.",
                    422,
                )
            effective_provider_key = (
                str(changes.get("provider_key") or provider.provider_key or "").strip().lower()
            )
            if submitted_key and effective_provider_key == "vertex_ai":
                try:
                    validate_vertex_service_account_json(submitted_key)
                except ValueError as error:
                    return error_response("validation_error", str(error), 422)
            if "options" in changes:
                try:
                    validate_provider_options(changes["options"])
                except ValueError as error:
                    return error_response("validation_error", str(error), 422)
                changes["options_json"] = changes.pop("options")
            if (
                "secret_ref" in changes
                and changes["secret_ref"]
                and credential_backend(changes["secret_ref"]) == "unavailable"
            ):
                return error_response(
                    "validation_error",
                    "The legacy secret_ref uses an unsupported credential scheme.",
                    422,
                )
            previous_retained = False
            configured_reference = None
            configured_reference_changed = False
            if requested_backend is not None or submitted_key:
                effective_options = changes.get("options_json", provider.options_json)
                key = llm_provider_credential_key(
                    effective_provider_key,
                    provider.id,
                    effective_options,
                )
                credential_kind = (
                    "credentials" if effective_provider_key == "vertex_ai" else "API key"
                )
                try:
                    configured = configure_credential_reference(
                        db_session,
                        database,
                        paths,
                        key=key,
                        label=f"{changes.get('label') or provider.label} {credential_kind}",
                        current_reference=previous_secret_ref,
                        backend=requested_backend or "database",
                        locator=requested_locator or "",
                        secret_value=submitted_key,
                        delete_previous=delete_previous,
                    )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    db_session.rollback()
                    return error_response("credential_unavailable", str(error), 422)
                configured_reference = configured.reference
                configured_reference_changed = True
                previous_retained = configured.previous_credential_retained
            elif clear_key:
                current_reference = previous_secret_ref
                if current_reference:
                    try:
                        delete_managed_reference(
                            db_session,
                            current_reference,
                            preserve_shared=False,
                        )
                    except RuntimeError as error:
                        db_session.rollback()
                        return error_response("credential_unavailable", str(error), 422)
                configured_reference_changed = True
            for key, value in changes.items():
                setattr(provider, key, value)
            if configured_reference_changed:
                provider.secret_ref = configured_reference
            provider.revision += 1
            provider.updated_at = utcnow()
            db_session.flush()
        result = _provider_payload(provider, database, paths)
        result["previous_credential_retained"] = previous_retained
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/providers/<provider_id>")
    @require_auth
    def provider_delete(provider_id: str):
        replacement_id = str(
            (request.get_json(silent=True) or {}).get("replacement_model_record_id") or ""
        )
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            active = db_session.scalar(
                select(ProviderModel).where(
                    ProviderModel.provider_id == provider_id,
                    ProviderModel.is_default.is_(True),
                )
            )
            if active is not None:
                replacement = (
                    db_session.get(ProviderModel, replacement_id) if replacement_id else None
                )
                if replacement is None or replacement.provider_id == provider_id:
                    return error_response(
                        "replacement_required",
                        "Select a default model from another provider before removing this provider.",
                        409,
                    )
                replacement.is_active = True
                replacement.is_default = True
            try:
                delete_managed_reference(
                    db_session,
                    str(
                        provider.secret_ref
                        or database_reference(provider_credential_key(provider.id))
                    ),
                )
            except RuntimeError as error:
                return error_response("credential_unavailable", str(error), 422)
            db_session.delete(provider)
        return "", 204

    @app.post("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_create(provider_id: str):
        payload = ModelCreate.model_validate(request.get_json(silent=True) or {})
        try:
            validate_provider_options(payload.options)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            if payload.is_default:
                for existing in db_session.scalars(select(ProviderModel)):
                    existing.is_default = False
            model = ProviderModel(
                provider_id=provider_id,
                model_id=payload.model_id,
                is_active=payload.is_active or payload.is_default,
                is_default=payload.is_default,
                default_temperature=payload.default_temperature,
                default_reasoning_effort=payload.default_reasoning_effort,
                input_cost_per_million=payload.input_cost_per_million,
                cached_input_cost_per_million=payload.cached_input_cost_per_million,
                output_cost_per_million=payload.output_cost_per_million,
                context_window_tokens=payload.context_window_tokens,
                max_output_tokens=payload.max_output_tokens,
                input_modalities_json=payload.input_modalities,
                output_modalities_json=payload.output_modalities,
                options_json=payload.options,
            )
            db_session.add(model)
            db_session.flush()
            result = _provider_model_payload(model)
        return jsonify(result), 201

    @app.post("/api/v1/providers/<provider_id>/test")
    @require_auth
    def provider_test(provider_id: str):
        from pandrator.logic.llm_handler import chat_completion_with_metadata

        from .provider_settings import build_llm_settings

        payload = ProviderTestRequest.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            selected = payload.model_id or db_session.scalar(
                select(ProviderModel.model_id).where(
                    ProviderModel.provider_id == provider_id,
                    ProviderModel.is_active.is_(True),
                    ProviderModel.is_default.is_(True),
                )
            )
            if not selected:
                selected = db_session.scalar(
                    select(ProviderModel.model_id)
                    .where(
                        ProviderModel.provider_id == provider_id,
                        ProviderModel.is_active.is_(True),
                    )
                    .order_by(ProviderModel.model_id)
                )
            if not selected:
                return error_response(
                    "validation_error",
                    "Activate at least one model before testing this provider.",
                    422,
                )
        try:
            settings, model_name = build_llm_settings(
                database, paths, requested_model=selected, requested_provider_id=provider_id
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        result = chat_completion_with_metadata(
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
            model_name=model_name,
            llm_settings=settings,
        )
        if not result.content:
            return error_response(
                "provider_test_failed",
                "The provider returned no usable response. Check its URL, secret reference, and model ID.",
                422,
            )
        return jsonify(
            {
                "ok": True,
                "model": result.model or model_name,
                "response": result.content[:80],
                "cost": result.cost,
                "cost_source": result.cost_source,
            }
        )

    @app.get("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_list(provider_id: str):
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            records = list(
                db_session.scalars(
                    select(ProviderModel)
                    .where(ProviderModel.provider_id == provider_id)
                    .order_by(ProviderModel.model_id)
                ).all()
            )
            return jsonify({"items": [_provider_model_payload(item) for item in records]})

    @app.patch("/api/v1/providers/<provider_id>/models/<model_record_id>")
    @require_auth
    def model_update(provider_id: str, model_record_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current model revision.",
                428,
            )
        payload = ModelUpdate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            model = db_session.get(ProviderModel, model_record_id)
            if model is None or model.provider_id != provider_id:
                return error_response("not_found", "Model not found.", 404)
            if model.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The model settings changed in another client.",
                    409,
                )
            changes = payload.model_dump(exclude_unset=True)
            if changes.get("is_active") is False and model.is_default:
                return error_response(
                    "validation_error",
                    "Choose another application default before deactivating this model.",
                    422,
                )
            if "options" in changes:
                try:
                    validate_provider_options(changes["options"])
                except ValueError as error:
                    return error_response("validation_error", str(error), 422)
            if changes.pop("is_default", False):
                for existing in db_session.scalars(select(ProviderModel)):
                    existing.is_default = existing.id == model.id
                changes["is_active"] = True
            if "input_modalities" in changes:
                changes["input_modalities_json"] = changes.pop("input_modalities")
            if "output_modalities" in changes:
                changes["output_modalities_json"] = changes.pop("output_modalities")
            if "options" in changes:
                changes["options_json"] = changes.pop("options")
            for key, value in changes.items():
                setattr(model, key, value)
            model.revision += 1
            db_session.flush()
            result = _provider_model_payload(model)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/providers/<provider_id>/models/<model_record_id>")
    @require_auth
    def model_delete(provider_id: str, model_record_id: str):
        body = request.get_json(silent=True) or {}
        replacement_record_id = str(body.get("replacement_model_record_id") or "")
        replacement_model_id = str(body.get("replacement_model_id") or "")
        with database.session() as db_session:
            model = db_session.get(ProviderModel, model_record_id)
            if model is None or model.provider_id != provider_id:
                return error_response("not_found", "Model not found.", 404)
            if model.is_default:
                replacement = (
                    db_session.get(ProviderModel, replacement_record_id)
                    if replacement_record_id
                    else None
                )
                if replacement is None and replacement_model_id:
                    replacement = db_session.scalar(
                        select(ProviderModel).where(
                            ProviderModel.provider_id == provider_id,
                            ProviderModel.model_id == replacement_model_id,
                        )
                    )
                if replacement is None or replacement.id == model.id:
                    return error_response(
                        "replacement_required",
                        "Select a replacement before deleting the active default model.",
                        409,
                    )
                replacement.is_active = True
                replacement.is_default = True
            db_session.delete(model)
        return "", 204

    @app.post("/api/v1/providers/<provider_id>/models/refresh")
    @require_auth
    def model_refresh(provider_id: str):
        from pandrator.logic.llm_handler import discover_provider_models

        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            existing = list(
                db_session.scalars(
                    select(ProviderModel).where(ProviderModel.provider_id == provider_id)
                ).all()
            )
            fallback_env = str(
                (provider.options_json or {}).get("api_key_env")
                or DEFAULT_PROVIDER_ENVS.get(provider.provider_key.lower(), "")
            )
            profile_id = str((provider.options_json or {}).get("profile_id") or "").strip().lower()
            share_credential = not bool(
                (provider.options_json or {}).get("is_custom")
                or profile_id in {"custom-openai", "lm-studio", "ollama"}
            )
            credential = resolve_provider_credential(
                database,
                paths,
                provider.provider_key,
                provider.secret_ref,
                fallback_environment_variable=fallback_env,
                shared=share_credential,
            )
            discovery = discover_provider_models(
                {
                    "provider": provider.provider_key,
                    "api_base": provider.base_url,
                    "api_key_env": credential.environment_variable,
                    "api_key": credential.value,
                    "models": [item.model_id for item in existing],
                }
            )
            detected = list(discovery.models) if discovery.source != "preserved" else []
            known = {item.model_id for item in existing}
            added = []
            for model_id in detected:
                if model_id in known:
                    continue
                model = ProviderModel(
                    provider_id=provider_id,
                    model_id=model_id,
                    is_active=False,
                    is_default=False,
                    input_modalities_json=["text"],
                    output_modalities_json=["text"],
                    options_json={"discovery_source": discovery.source},
                )
                db_session.add(model)
                added.append(model_id)
            return jsonify(
                {
                    "detected": detected,
                    "added": added,
                    "preserved": sorted(known),
                    "source": discovery.source,
                    "endpoint": discovery.endpoint,
                    "warning": discovery.warning,
                }
            )

    @app.get("/api/v1/credentials")
    @require_auth
    def credential_list():
        return jsonify({"items": auxiliary_profiles(database, paths)})

    @app.put("/api/v1/credentials/<credential_id>")
    @require_auth
    def credential_update(credential_id: str):
        profile = next(
            (item for item in AUXILIARY_CREDENTIALS if item["id"] == credential_id),
            None,
        )
        if profile is None:
            return error_response("not_found", "Credential setting not found.", 404)
        payload = CredentialUpdate.model_validate(request.get_json(silent=True) or {})
        submitted_key = str(payload.api_key or "").strip()
        if submitted_key and payload.clear:
            return error_response(
                "validation_error",
                "Choose either a replacement API key or remove the current key.",
                422,
            )
        if not submitted_key and not payload.clear and payload.credential_backend is None:
            return error_response(
                "validation_error",
                "Enter an API key, choose an external credential backend, or remove the saved credential.",
                422,
            )
        key = auxiliary_credential_key(credential_id)
        configured = None
        try:
            with database.session() as db_session:
                references = auxiliary_reference_map(db_session)
                current_reference = references.get(
                    credential_id,
                    database_reference(key),
                )
                if payload.clear:
                    delete_managed_reference(
                        db_session,
                        current_reference,
                        preserve_shared=False,
                    )
                    set_auxiliary_reference(db_session, credential_id, None)
                else:
                    configured = configure_credential_reference(
                        db_session,
                        database,
                        paths,
                        key=key,
                        label=f"{profile['label']} API key",
                        current_reference=current_reference,
                        backend=payload.credential_backend or "database",
                        locator=payload.credential_reference or "",
                        secret_value=submitted_key,
                        delete_previous=payload.delete_previous_credential,
                    )
                    set_auxiliary_reference(
                        db_session,
                        credential_id,
                        configured.reference,
                    )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            return error_response("credential_unavailable", str(error), 422)
        updated = next(
            item for item in auxiliary_profiles(database, paths) if item["id"] == credential_id
        )
        if configured is not None:
            updated["previous_credential_retained"] = configured.previous_credential_retained
        return jsonify(updated)
