"""HTTP routes for generation plans, runs, and output assemblies."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import select

from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Artifact, OutputAssembly, SessionRecord
from .route_context import RouteContext
from .schemas import (
    GenerationPlanCreate,
    GenerationPlanTopologyRequest,
    GenerationSegmentBatchUpdate,
    GenerationSegmentUpdate,
    GenerationStartRequest,
    OutputAssemblyCreateRequest,
    OutputMixPreviewRequest,
)
from .settings_policy import RevisionConflict as WorkspaceRevisionConflict
from .source_resolution import resolve_media_source


def register_generation_routes(
    app: DomainBlueprints,
    context: RouteContext,
    *,
    idempotency: MutationIdempotency,
) -> None:
    services = context.services
    database = services.database
    generation = services.generation
    sessions = services.sessions
    jobs = services.jobs
    error_response = context.guards.error_response
    inline_credential_error = context.guards.inline_credential_error
    require_auth = context.guards.require_auth
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure

    @app.post("/api/v1/sessions/<session_id>/generation-plan")
    @require_auth
    def generation_plan_create(session_id: str):
        payload = GenerationPlanCreate.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.settings):
            return rejected
        try:
            result = generation.create_plan(
                session_id,
                source_revision_id=payload.source_revision_id,
                segments=[item.model_dump() for item in payload.segments],
                settings=payload.settings,
            )
        except KeyError:
            return error_response("not_found", "Session or source revision not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

    @app.post("/api/v1/sessions/<session_id>/generation-plan/topology")
    @require_auth
    def generation_plan_topology(session_id: str):
        payload = GenerationPlanTopologyRequest.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if not raw_etag:
            return error_response(
                "precondition_required",
                "If-Match must contain the current plan revision ID.",
                428,
            )
        if raw_etag != payload.expected_revision_id:
            return error_response(
                "revision_conflict",
                "The request revision does not match If-Match.",
                409,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is None:
            return error_response(
                "idempotency_key_required",
                "Speech-block topology revisions require Idempotency-Key.",
                400,
            )
        operation = payload.model_dump(exclude={"expected_revision_id"}, exclude_none=True)
        try:
            with database.immediate_session() as db_session:
                reservation = services.idempotency.begin(
                    db_session,
                    principal=idempotency.principal(),
                    operation_id="reviseGenerationPlanTopology",
                    idempotency_key=idempotency_key,
                    payload={
                        "session_id": session_id,
                        "expected_revision_id": payload.expected_revision_id,
                        **operation,
                    },
                )
                if reservation.response is not None:
                    result, status_code = reservation.response
                    response = jsonify(result)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
                    return response
                result = generation.revise_topology_in_session(
                    db_session,
                    session_id,
                    payload.expected_revision_id,
                    operation,
                )
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=result,
                    status_code=201,
                    resource_kind="generation_plan_revision",
                    resource_id=result["plan_revision_id"],
                )
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            if isinstance(error, WorkspaceRevisionConflict):
                return error_response("revision_conflict", str(error), 409)
            return idempotency_failure(error)
        except KeyError:
            return error_response(
                "not_found",
                "Session, generation plan revision, or segment not found.",
                404,
            )
        response = jsonify(result)
        response.status_code = 201
        response.headers["Idempotency-Replayed"] = "false"
        response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/generation-plan/revisions")
    @require_auth
    def generation_plan_revisions(session_id: str):
        from .generation_review import parse_summary_flag, revision_history

        try:
            result = revision_history(
                database,
                session_id,
                limit=int(request.args.get("limit", 50)),
                before_revision_number=int(request.args["before_revision_number"])
                if "before_revision_number" in request.args
                else None,
                include_audio_reuse=not parse_summary_flag(request.args.get("summary")),
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.get("/api/v1/sessions/<session_id>/generation-plan/revisions/<revision_id>")
    @require_auth
    def generation_plan_revision_detail(session_id: str, revision_id: str):
        from .generation_review import revision_detail

        try:
            return jsonify(revision_detail(database, session_id, revision_id))
        except KeyError:
            return error_response("not_found", "Session or speech-plan revision not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.post("/api/v1/sessions/<session_id>/generation-plan/topology/batch")
    @require_auth
    def generation_plan_topology_batch(session_id: str):
        from .generation_review import revise_topology_batch_in_session
        from .schemas import GenerationPlanBatchRequest

        payload = GenerationPlanBatchRequest.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if not raw_etag:
            return error_response(
                "precondition_required",
                "If-Match must contain the current speech-plan revision ID.",
                428,
            )
        if raw_etag != payload.expected_revision_id:
            return error_response(
                "revision_conflict", "The request revision does not match If-Match.", 409
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is None:
            return error_response(
                "idempotency_key_required", "Atomic topology batches require Idempotency-Key.", 400
            )
        request_payload = {"session_id": session_id, **payload.model_dump(exclude_none=True)}
        try:
            with database.immediate_session() as db_session:
                reservation = services.idempotency.begin(
                    db_session,
                    principal=idempotency.principal(),
                    operation_id="reviseGenerationPlanTopologyBatch",
                    idempotency_key=idempotency_key,
                    payload=request_payload,
                )
                if reservation.response is not None:
                    result, status_code = reservation.response
                    response = jsonify(result)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
                    return response
                result = revise_topology_batch_in_session(
                    generation,
                    db_session,
                    session_id,
                    payload.expected_revision_id,
                    [operation.model_dump(exclude_none=True) for operation in payload.operations],
                )
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=result,
                    status_code=201,
                    resource_kind="generation_plan_revision",
                    resource_id=result["plan_revision_id"],
                )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        except KeyError:
            return error_response("not_found", "Session, revision, or segment not found.", 404)
        response = jsonify(result)
        response.status_code = 201
        response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/generation-segments")
    @require_auth
    def generation_segment_list(session_id: str):
        def optional_bool_arg(name: str, *, default: bool | None = None) -> bool | None:
            raw = request.args.get(name)
            if raw is None:
                return default
            normalized = raw.strip().lower()
            if normalized not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false.")
            return normalized == "true"

        try:
            marked = optional_bool_arg("marked")
            result = generation.list_segments(
                session_id,
                cursor=request.args.get("cursor", 0, type=int),
                limit=request.args.get("limit", 100, type=int),
                status=request.args.get("status"),
                marked=marked,
                verification=request.args.get("verification"),
                generation_run_id=request.args.get("generation_run_id"),
                plan_revision_id=request.args.get("plan_revision_id"),
                view=request.args.get("view", "full"),
                fields=request.args["fields"].split(",") if "fields" in request.args else None,
                end_ordinal=int(request.args["end_ordinal"])
                if "end_ordinal" in request.args
                else None,
                around_ordinal=int(request.args["around_ordinal"])
                if "around_ordinal" in request.args
                else None,
                source_cue_id=request.args.get("source_cue_id"),
                radius=int(request.args.get("radius", 2)),
                q=request.args.get("q"),
                match_case=optional_bool_arg("match_case", default=False) is True,
                whole_word=optional_bool_arg("whole_word", default=False) is True,
                text_field=request.args.get("text_field", "text"),
                boundary_flags=optional_bool_arg("boundary_flags"),
            )
        except KeyError:
            return error_response(
                "not_found",
                "Session or generation run not found.",
                404,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.patch("/api/v1/sessions/<session_id>/generation-segments")
    @require_auth
    def generation_segments_update(session_id: str):
        payload = GenerationSegmentBatchUpdate.model_validate(request.get_json(silent=True) or {})
        clearable = {"optimized_text", "voice_id", "voice", "language"}
        updates = []
        for item in payload.updates:
            changes = item.changes.model_dump(exclude_unset=True)
            if not changes:
                return error_response(
                    "validation_error",
                    "Every generation segment update requires at least one change.",
                    422,
                )
            null_fields = [
                key for key, value in changes.items() if value is None and key not in clearable
            ]
            if null_fields:
                return error_response(
                    "validation_error",
                    f"{', '.join(null_fields)} cannot be null.",
                    422,
                )
            updates.append(
                {
                    "id": item.id,
                    "revision": item.revision,
                    "changes": changes,
                }
            )
        try:
            result = generation.update_segments(session_id, updates)
        except KeyError:
            return error_response(
                "not_found",
                "Session or generation segment not found.",
                404,
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.patch("/api/v1/generation-segments/<segment_id>")
    @require_auth
    def generation_segment_update(segment_id: str):
        payload = GenerationSegmentUpdate.model_validate(request.get_json(silent=True) or {})
        changes = payload.model_dump(exclude_unset=True)
        clearable = {"optimized_text", "voice_id", "voice", "language"}
        null_fields = [
            key for key, value in changes.items() if value is None and key not in clearable
        ]
        if null_fields:
            return error_response(
                "validation_error",
                f"{', '.join(null_fields)} cannot be null.",
                422,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current segment revision.",
                428,
            )
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="updateGenerationSegment",
                        idempotency_key=idempotency_key,
                        payload={
                            "segment_id": segment_id,
                            "expected_revision": expected,
                            "changes": changes,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    result = generation.update_segment_in_session(
                        db_session, segment_id, expected, changes
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="generation_segment",
                        resource_id=segment_id,
                    )
            except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
                if isinstance(error, WorkspaceRevisionConflict):
                    return error_response("revision_conflict", str(error), 409)
                return idempotency_failure(error)
            except KeyError:
                return error_response("not_found", "Generation segment not found.", 404)
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            # Explicit null clears a segment override back to the inherited
            # session value; omitted fields remain unchanged.
            result = generation.update_segment(segment_id, expected, changes)
        except KeyError:
            return error_response("not_found", "Generation segment not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/generation-segments/<segment_id>/takes/<take_id>/select")
    @require_auth
    def generation_take_select(segment_id: str, take_id: str):
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current segment revision.",
                428,
            )
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="selectGenerationTake",
                        idempotency_key=idempotency_key,
                        payload={
                            "segment_id": segment_id,
                            "take_id": take_id,
                            "expected_revision": expected,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    result = generation.select_take_in_session(
                        db_session, segment_id, take_id, expected
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="generation_segment",
                        resource_id=segment_id,
                    )
            except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
                if isinstance(error, WorkspaceRevisionConflict):
                    return error_response("revision_conflict", str(error), 409)
                if isinstance(error, ValueError):
                    return error_response("invalid_take", str(error), 409)
                return idempotency_failure(error)
            except KeyError:
                return error_response(
                    "not_found", "Generation segment or audio take not found.", 404
                )
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            result = generation.select_take(segment_id, take_id, expected)
        except KeyError:
            return error_response("not_found", "Generation segment or audio take not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("invalid_take", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/generation-runs/latest")
    @require_auth
    def generation_run_latest(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"item": generation.latest_run(session_id)})

    @app.get("/api/v1/sessions/<session_id>/generation-runs")
    @require_auth
    def generation_run_list(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        raw_limit = request.args.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else None
            if limit is not None and not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            return error_response(
                "validation_error", "limit must be an integer from 1 through 100", 422
            )
        include_repairs = request.args.get("include_repairs", "true")
        if include_repairs not in {"true", "false"}:
            return error_response("validation_error", "include_repairs must be true or false", 422)
        return jsonify(
            {
                "items": generation.list_runs(
                    session_id, include_repairs=include_repairs == "true", limit=limit
                )
            }
        )

    @app.post("/api/v1/sessions/<session_id>/generation-runs")
    @require_auth
    def generation_run_start(session_id: str):
        payload = GenerationStartRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.run_override):
            return rejected
        if rejected := inline_credential_error(payload.selected_segment_override):
            return rejected
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            principal = idempotency.principal()
            request_payload = {
                "session_id": session_id,
                **payload.model_dump(mode="json"),
            }
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=principal,
                        operation_id="startGenerationRun",
                        idempotency_key=idempotency_key,
                        payload=request_payload,
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except ValueError as error:
                return error_response("generation_unavailable", str(error), 409)

            reservation_id = reservation.record.id

            def abandon_generation_reservation() -> None:
                try:
                    with database.immediate_session() as db_session:
                        fresh = services.idempotency.load_in_progress(
                            db_session, reservation_id, principal=principal
                        )
                        services.idempotency.abandon_in_progress(db_session, fresh)
                except (IdempotencyInProgress, KeyError):
                    # A completed reservation must never be deleted.  A process
                    # crash can also intentionally leave its marker for stale
                    # recovery rather than turning it into a second execution.
                    pass

            try:
                prepared = generation.prepare_start(
                    session_id,
                    run_override=payload.run_override,
                    selected_segment_override=payload.selected_segment_override,
                    segment_ids=payload.segment_ids,
                    generation_run_id=payload.generation_run_id,
                    settings_source_run_id=payload.settings_source_run_id,
                    operation=payload.operation,
                    speech_plan_revision_id=payload.speech_plan_revision_id,
                    stale_only=payload.stale_only,
                    missing_only=payload.missing_only,
                    expected_selection_hash=payload.expected_selection_hash,
                )
            except KeyError:
                abandon_generation_reservation()
                return error_response("not_found", "Session not found.", 404)
            except ValueError as error:
                abandon_generation_reservation()
                return error_response("generation_unavailable", str(error), 409)
            except Exception:
                abandon_generation_reservation()
                raise

            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.load_in_progress(
                        db_session, reservation_id, principal=principal
                    )
                    result = generation.start_in_session(db_session, session_id, prepared=prepared)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="generation_run",
                        resource_id=str(result["id"]),
                    )
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except KeyError:
                abandon_generation_reservation()
                return error_response("not_found", "Session not found.", 404)
            except ValueError as error:
                abandon_generation_reservation()
                return error_response("generation_unavailable", str(error), 409)
            except Exception:
                abandon_generation_reservation()
                raise
            return jsonify(result), 202
        try:
            result = generation.start(
                session_id,
                run_override=payload.run_override,
                selected_segment_override=payload.selected_segment_override,
                segment_ids=payload.segment_ids,
                generation_run_id=payload.generation_run_id,
                settings_source_run_id=payload.settings_source_run_id,
                operation=payload.operation,
                speech_plan_revision_id=payload.speech_plan_revision_id,
                stale_only=payload.stale_only,
                missing_only=payload.missing_only,
                expected_selection_hash=payload.expected_selection_hash,
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("generation_unavailable", str(error), 409)
        return jsonify(result), 202

    @app.post("/api/v1/sessions/<session_id>/generation-runs/preview")
    @require_auth
    def generation_run_preview(session_id: str):
        payload = GenerationStartRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.run_override):
            return rejected
        if rejected := inline_credential_error(payload.selected_segment_override):
            return rejected
        try:
            result = generation.preview_selection(
                session_id,
                run_override=payload.run_override,
                selected_segment_override=payload.selected_segment_override,
                segment_ids=payload.segment_ids,
                generation_run_id=payload.generation_run_id,
                settings_source_run_id=payload.settings_source_run_id,
                operation=payload.operation,
                speech_plan_revision_id=payload.speech_plan_revision_id,
                stale_only=payload.stale_only,
                missing_only=payload.missing_only,
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("generation_unavailable", str(error), 409)
        return jsonify(result), 200

    @app.post("/api/v1/generation-runs/<run_id>/pause")
    @require_auth
    def generation_run_pause(run_id: str):
        try:
            return jsonify(generation.request_pause(run_id)), 202
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)

    @app.post("/api/v1/generation-runs/<run_id>/resume")
    @require_auth
    def generation_run_resume(run_id: str):
        try:
            return jsonify(generation.resume(run_id)), 202
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)

    @app.post("/api/v1/generation-runs/<run_id>/cancel")
    @require_auth
    def generation_run_cancel(run_id: str):
        try:
            return jsonify(generation.cancel(run_id)), 202
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)

    @app.delete("/api/v1/generation-runs/<run_id>")
    @require_auth
    def generation_run_delete(run_id: str):
        try:
            generation.delete_run(run_id)
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)
        return "", 204

    @app.get("/api/v1/sessions/<session_id>/output-assemblies/latest")
    @require_auth
    def output_assembly_latest(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"item": generation.latest_assembly(session_id)})

    @app.post("/api/v1/sessions/<session_id>/output-assemblies")
    @require_auth
    def output_assembly_create(session_id: str):
        payload = OutputAssemblyCreateRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.run_override):
            return rejected
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="createOutputAssembly",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            **payload.model_dump(mode="json"),
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    prepared = generation.prepare_assembly(
                        session_id, run_override=payload.run_override
                    )
                    result = generation.create_assembly_in_session(
                        db_session,
                        session_id,
                        generation_run_id=payload.generation_run_id,
                        prepared=prepared,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="output_assembly",
                        resource_id=str(result["id"]),
                    )
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except KeyError:
                return error_response("not_found", "Session or generation run not found.", 404)
            except ValueError as error:
                return error_response("assembly_unavailable", str(error), 409)
            return jsonify(result), 202
        try:
            result = generation.create_assembly(
                session_id,
                generation_run_id=payload.generation_run_id,
                run_override=payload.run_override,
            )
        except KeyError:
            return error_response("not_found", "Session or generation run not found.", 404)
        except ValueError as error:
            return error_response("assembly_unavailable", str(error), 409)
        return jsonify(result), 202

    @app.post("/api/v1/sessions/<session_id>/output-mix-preview")
    @require_auth
    def output_mix_preview(session_id: str):
        """Queue a bounded audio preview from server-resolved mix inputs."""

        payload = OutputMixPreviewRequest.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            record = db_session.get(SessionRecord, session_id)
            if record is None:
                return error_response("not_found", "Session not found.", 404)
            if record.workflow_kind != "voiceover":
                return error_response(
                    "mix_preview_unavailable",
                    "Soundtrack mix previews are available for voiceover sessions.",
                    409,
                )
            source_resolution = resolve_media_source(db_session, session_id)
            source = source_resolution.artifact
            if source is None or not source_resolution.has_audio:
                return error_response(
                    "mix_preview_unavailable",
                    "Attach an audio or video source before previewing the soundtrack mix.",
                    409,
                )
            assembly = db_session.scalar(
                select(OutputAssembly)
                .where(
                    OutputAssembly.session_id == session_id,
                    OutputAssembly.generation_run_id == payload.generation_run_id,
                    OutputAssembly.status == "completed",
                    OutputAssembly.artifact_id.is_not(None),
                )
                .order_by(OutputAssembly.created_at.desc())
            )
            if assembly is None:
                return error_response(
                    "mix_preview_unavailable",
                    "Assemble the selected audio version before previewing its soundtrack mix.",
                    409,
                )
            dubbing = db_session.get(Artifact, assembly.artifact_id)
            if dubbing is None or dubbing.state == "deleted":
                return error_response(
                    "mix_preview_unavailable",
                    "The selected audio version's assembly is unavailable.",
                    409,
                )
            source_artifact_id = source.id
            dubbing_artifact_id = dubbing.id

        mix_fields = (
            "mix_source_gain_db",
            "mix_voice_gain_db",
            "mix_voice_lufs",
            "mix_ducking",
            "mix_attack_ms",
            "mix_release_ms",
        )
        job = jobs.enqueue(
            "output.mix_preview",
            {
                "session_id": session_id,
                "generation_run_id": payload.generation_run_id,
                "source_artifact_id": source_artifact_id,
                "dubbing_artifact_id": dubbing_artifact_id,
                "start_seconds": payload.start_seconds,
                "duration_seconds": payload.duration_seconds,
                "settings": {field: getattr(payload, field) for field in mix_fields},
            },
            session_id=session_id,
            resource_keys=[f"session:{session_id}"],
        )
        return jsonify(_job_payload(job)), 202
