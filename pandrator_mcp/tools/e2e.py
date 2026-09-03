"""Safe host-file import, catalog-backed TTS, export planning, and delivery."""

from __future__ import annotations

import errno
import hashlib
import mimetypes
import os
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from ..context import McpRuntime
from ..errors import NextAction, PandratorMcpError
from ..network_policy import TargetMode
from ..results import ToolOutcome
from ..schemas.e2e import (
    BrowseLocalSourcesInput,
    ConfigureTtsInput,
    DownloadArtifactInput,
    ImportLocalSourceInput,
    ListGenerationRunsInput,
    PlanExportVariantInput,
    TtsCatalogInput,
)


def _named_source_root(runtime: McpRuntime, name: str) -> tuple[str, Path]:
    profile = runtime.profile
    if profile is None:
        raise PandratorMcpError(
            "application_unavailable",
            "The selected target profile is unavailable.",
        )
    match = next(
        (item for item in profile.local_source_roots if item.name.casefold() == name.casefold()),
        None,
    )
    if match is None:
        raise PandratorMcpError(
            "not_found",
            "That local source root is not configured.",
            details={"available_roots": [item.name for item in profile.local_source_roots]},
        )
    try:
        root = Path(match.path).resolve(strict=True)
    except OSError as error:
        raise PandratorMcpError(
            "source_unavailable",
            "The configured local source root is currently unavailable.",
            details={"root": match.name},
            retryable=True,
        ) from error
    if not root.is_dir():
        raise PandratorMcpError(
            "source_unavailable",
            "The configured local source root is not a directory.",
            details={"root": match.name},
        )
    return match.name, root


def _relative_parts(value: str, *, allow_empty: bool) -> tuple[str, ...]:
    raw = str(value or "")
    if "\\" in raw or "\x00" in raw:
        raise PandratorMcpError(
            "validation_error",
            "Local source paths must use safe POSIX-style relative components.",
        )
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        if allow_empty and not raw:
            return ()
        raise PandratorMcpError(
            "validation_error",
            "Local source paths must stay inside their configured root.",
        )
    if not relative.parts and not allow_empty:
        raise PandratorMcpError("validation_error", "A local source file is required.")
    return tuple(relative.parts)


def _contained(root: Path, parts: tuple[str, ...], *, file_required: bool) -> Path:
    candidate = root.joinpath(*parts).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise PandratorMcpError(
            "network_policy_denied",
            "The selected path escapes its configured local source root.",
        ) from error
    if file_required and not candidate.is_file():
        raise PandratorMcpError("not_found", "The selected local source is not a file.")
    if not file_required and not candidate.is_dir():
        raise PandratorMcpError("not_found", "The selected local directory was not found.")
    return candidate


def _open_contained_file(root: Path, parts: tuple[str, ...]) -> int:
    """Open a regular file without following any relative-path symlink."""

    base_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_flags = base_flags | getattr(os, "O_DIRECTORY", 0) | no_follow
    directory_fd: int | None = None
    try:
        directory_fd = os.open(root, directory_flags)
        for part in parts[:-1]:
            next_fd = os.open(part, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        descriptor = os.open(
            parts[-1],
            base_flags | no_follow,
            dir_fd=directory_fd,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PandratorMcpError(
                "network_policy_denied",
                "The selected local source traverses a symbolic link.",
            ) from error
        if error.errno == errno.ENOENT:
            raise PandratorMcpError(
                "not_found",
                "The selected local source was not found.",
            ) from error
        raise PandratorMcpError(
            "source_unavailable",
            "The selected local source could not be opened.",
            retryable=True,
        ) from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return descriptor


def browse_local_sources(
    runtime: McpRuntime,
    arguments: BrowseLocalSourcesInput,
) -> dict[str, Any]:
    profile = runtime.profile
    if profile is None:
        raise PandratorMcpError(
            "application_unavailable",
            "The selected target profile is unavailable.",
        )
    if arguments.root is None:
        return {
            "schema_version": "1",
            "roots": [
                {
                    "name": item.name,
                    "available": Path(item.path).is_dir(),
                }
                for item in profile.local_source_roots
            ],
        }
    root_name, root = _named_source_root(runtime, arguments.root)
    base_parts = _relative_parts(arguments.directory, allow_empty=True)
    base = _contained(root, base_parts, file_required=False)
    query = str(arguments.query or "").casefold()
    entries: list[dict[str, Any]] = []
    scanned = 0
    iterator = base.rglob("*") if arguments.recursive else base.iterdir()
    for candidate in iterator:
        scanned += 1
        if scanned > 5_000:
            break
        try:
            if candidate.is_symlink():
                continue
            relative = candidate.relative_to(root)
            if len(relative.parts) > len(base_parts) + (5 if arguments.recursive else 1):
                continue
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            info = resolved.stat()
        except (OSError, ValueError):
            continue
        if query and query not in candidate.name.casefold():
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            continue
        entries.append(
            {
                "relative_path": relative.as_posix(),
                "name": candidate.name,
                "kind": "directory" if stat.S_ISDIR(info.st_mode) else "file",
                "size_bytes": info.st_size if stat.S_ISREG(info.st_mode) else None,
                "modified_at_unix": info.st_mtime,
            }
        )
    entries.sort(
        key=(
            (lambda item: (-float(item["modified_at_unix"]), str(item["relative_path"])))
            if arguments.sort == "modified_desc"
            else (lambda item: str(item["relative_path"]).casefold())
        )
    )
    return {
        "schema_version": "1",
        "root": root_name,
        "directory": PurePosixPath(*base_parts).as_posix() if base_parts else "",
        "items": entries[: arguments.limit],
        "truncated": len(entries) > arguments.limit or scanned > 5_000,
    }


def _derived_upload_key(arguments: ImportLocalSourceInput, digest: str) -> str:
    material = "\x00".join(
        (
            arguments.idempotency_key,
            arguments.root.casefold(),
            arguments.relative_path,
            digest,
        )
    )
    return "mcp-upload:" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def import_local_source(
    runtime: McpRuntime,
    arguments: ImportLocalSourceInput,
) -> ToolOutcome:
    root_name, root = _named_source_root(runtime, arguments.root)
    parts = _relative_parts(arguments.relative_path, allow_empty=False)
    source_name = parts[-1]
    descriptor = _open_contained_file(root, parts)
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
                raise PandratorMcpError(
                    "source_invalid",
                    "The selected local source must be a non-empty regular file.",
                )
            digest = hashlib.sha256()
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
            content_hash = digest.hexdigest()
            application = runtime.require_application()
            current_sources = application.list_sources().get("items")
            existing = next(
                (
                    item
                    for item in (current_sources or [])
                    if isinstance(item, dict)
                    and item.get("state") == "current"
                    and item.get("content_hash") == content_hash
                    and int(item.get("size_bytes") or -1) == before.st_size
                ),
                None,
            )
            reused = existing is not None
            if existing is not None:
                source_asset_id = str(existing["id"])
                artifact_id = str(existing.get("artifact_id") or "")
            else:
                upload = application.initialize_upload(
                    filename=source_name,
                    size_bytes=before.st_size,
                    mime_type=mimetypes.guess_type(source_name)[0],
                    sha256=content_hash,
                    idempotency_key=_derived_upload_key(arguments, content_hash),
                )
                result = upload.get("result") if upload.get("state") == "completed" else None
                if not isinstance(result, dict):
                    upload_id = str(upload.get("id") or "")
                    chunk_size = int(upload.get("chunk_size") or 0)
                    if not upload_id or chunk_size <= 0:
                        raise PandratorMcpError(
                            "downstream_unavailable",
                            "Pandrator returned an invalid resumable-upload state.",
                        )
                    received = {int(value) for value in upload.get("received") or []}
                    handle.seek(0)
                    for index in range(int(upload.get("chunk_count") or 0)):
                        body = handle.read(chunk_size)
                        if index in received:
                            continue
                        application.upload_chunk(
                            upload_id,
                            index,
                            body,
                            sha256=hashlib.sha256(body).hexdigest(),
                        )
                    after = os.fstat(handle.fileno())
                    if (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise PandratorMcpError(
                            "source_changed",
                            "The local source changed while it was being uploaded.",
                        )
                    result = application.complete_upload(upload_id)
                source_asset_id = str(result.get("source_asset_id") or "")
                artifact_id = str(result.get("artifact_id") or "")
                if not source_asset_id:
                    raise PandratorMcpError(
                        "downstream_unavailable",
                        "The completed upload did not create a source asset.",
                    )
    except Exception:
        # os.fdopen owns the descriptor after construction; before that point
        # this guard is the only possible close path.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    attachment = runtime.require_application().attach_existing_source(
        arguments.session_id,
        source_asset_id=source_asset_id,
        role=arguments.role,
        expected_session_revision=arguments.expected_session_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result={
            "schema_version": "1",
            "root": root_name,
            "relative_path": PurePosixPath(*parts).as_posix(),
            "artifact_id": artifact_id,
            "source_asset_id": source_asset_id,
            "content_hash": content_hash,
            "size_bytes": before.st_size,
            "reused_existing_source": reused,
            "attachment": attachment,
        },
        next_actions=[
            NextAction(
                tool="pandrator_get_workflow",
                arguments={"session_id": arguments.session_id},
                reason="Inspect the attached source and the workflow prerequisites.",
            )
        ],
    )


def _normalized_id(value: object) -> str:
    return str(value or "").strip().casefold().replace("-", "_").replace(" ", "_")


def _safe_tts_service(service: dict[str, Any]) -> dict[str, Any]:
    return {
        key: service.get(key)
        for key in (
            "id",
            "name",
            "kind",
            "available",
            "online",
            "availability_reason",
            "models",
            "default_model",
            "voices",
            "default_voice",
            "default_voices",
            "default_voices_by_language",
            "voice_catalogues",
            "voice_metadata",
            "languages",
            "generation_prompt_models",
            "supports_voice_cloning",
            "supports_dynamic_catalog",
            "supports_batch_synthesis",
            "batch_synthesis",
        )
        if key in service
    }


def tts_catalog(runtime: McpRuntime, arguments: TtsCatalogInput) -> dict[str, Any]:
    application = runtime.require_application()
    payload = application.tts_catalog(refresh=arguments.refresh)
    services = [
        _safe_tts_service(item)
        for item in payload.get("services") or []
        if isinstance(item, dict)
        and (
            arguments.service_id is None
            or _normalized_id(item.get("id") or item.get("name"))
            == _normalized_id(arguments.service_id)
        )
    ]
    voices = []
    raw_voices = application.list_voices().get("items")
    for item in raw_voices or []:
        if not isinstance(item, dict):
            continue
        registrations = {}
        providers = (item.get("metadata_json") or {}).get("providers")
        if isinstance(providers, dict):
            for service_id, registration in list(providers.items())[:40]:
                if isinstance(registration, dict):
                    registrations[str(service_id)] = {
                        key: registration.get(key)
                        for key in ("status", "voice_id", "stale_reason")
                        if key in registration
                    }
        voices.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "language": item.get("language"),
                "registrations": registrations,
                "revision": item.get("revision"),
            }
        )
    return {
        "schema_version": "1",
        "default_service": payload.get("default_service"),
        "revision": payload.get("revision"),
        "services": services,
        "managed_voices": voices,
    }


def _catalog_value(values: Any, requested: str) -> str | None:
    if not isinstance(values, list):
        return None
    return next(
        (str(value) for value in values if str(value).casefold() == requested.casefold()),
        None,
    )


def configure_tts(runtime: McpRuntime, arguments: ConfigureTtsInput) -> ToolOutcome:
    catalog = tts_catalog(runtime, TtsCatalogInput(service_id=arguments.service_id))
    if not catalog["services"]:
        raise PandratorMcpError("not_found", "The requested TTS service is not configured.")
    service = catalog["services"][0]
    if service.get("available") is False:
        raise PandratorMcpError(
            "source_unavailable",
            str(service.get("availability_reason") or "The requested TTS service is unavailable."),
        )
    service_id = str(service.get("id") or arguments.service_id)
    models = service.get("models") if isinstance(service.get("models"), list) else []
    model = ""
    if arguments.model:
        model = _catalog_value(models, arguments.model) or ""
        if (
            not model
            and arguments.model.casefold() != str(service.get("default_model") or "").casefold()
        ):
            raise PandratorMcpError(
                "validation_error",
                "The requested TTS model is not advertised by that service.",
                details={"available_models": models[:100]},
            )
        model = model or arguments.model
    else:
        model = str(service.get("default_model") or (models[0] if models else ""))
    voice = ""
    if arguments.voice:
        catalogue = service.get("voice_catalogues")
        native: list[Any] = []
        if isinstance(catalogue, dict) and model:
            native = catalogue.get(model) or []
        if not native:
            native = service.get("voices") or []
        voice = _catalog_value(native, arguments.voice) or ""
        if not voice:
            managed = next(
                (
                    item
                    for item in catalog["managed_voices"]
                    if str(item.get("id") or "").casefold() == arguments.voice.casefold()
                    or str(item.get("name") or "").casefold() == arguments.voice.casefold()
                ),
                None,
            )
            registration = None
            if managed:
                registration = next(
                    (
                        value
                        for key, value in (managed.get("registrations") or {}).items()
                        if _normalized_id(key) == _normalized_id(service_id)
                    ),
                    None,
                )
            if (
                not isinstance(registration, dict)
                or registration.get("status") != "ready"
                or not registration.get("voice_id")
            ):
                raise PandratorMcpError(
                    "validation_error",
                    "The requested voice is neither native to this service nor a ready managed registration.",
                )
            voice = str(registration["voice_id"])
    else:
        defaults = service.get("default_voices")
        language_defaults = service.get("default_voices_by_language")
        model_defaults = None
        if isinstance(language_defaults, dict) and model:
            model_defaults = next(
                (
                    value
                    for key, value in language_defaults.items()
                    if str(key).casefold() == model.casefold()
                ),
                None,
            )
        language_voice = None
        if isinstance(model_defaults, dict) and arguments.language:
            language_voice = next(
                (
                    value
                    for key, value in model_defaults.items()
                    if str(key).casefold() == arguments.language.casefold()
                ),
                None,
            )
        voice = str(
            language_voice
            or (defaults.get(model) if isinstance(defaults, dict) and model else "")
            or service.get("default_voice")
            or ""
        )
    application = runtime.require_application()
    current = application.get_session_settings(arguments.session_id, "tts")
    override = dict(current.get("override") or {})
    override.update({"service": service_id})
    if model:
        override["model"] = model
    if voice:
        override["voice"] = voice
    if arguments.language:
        override["language"] = arguments.language
    if arguments.style_instructions is not None:
        override["generation_prompt"] = arguments.style_instructions
        override["openai_audio_instructions"] = arguments.style_instructions
    result = application.update_session_settings(
        arguments.session_id,
        section="tts",
        value=override,
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    return ToolOutcome(
        result={
            "schema_version": "1",
            "selection": {
                "service_id": service_id,
                "model": model or None,
                "voice": voice or None,
                "language": arguments.language,
                "style_instructions_applied": arguments.style_instructions is not None,
            },
            "settings_revision": result.get("revision"),
        },
        next_actions=[
            NextAction(
                tool="pandrator_plan_workflow",
                arguments={
                    "session_id": arguments.session_id,
                    "target_stage": "generate_audio",
                },
                reason="Review the exact generation plan and provider disclosures.",
            )
        ],
    )


def list_generation_runs(
    runtime: McpRuntime,
    arguments: ListGenerationRunsInput,
) -> dict[str, Any]:
    payload = runtime.require_application().list_generation_runs(arguments.session_id)
    items = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                key: item.get(key)
                for key in (
                    "id",
                    "label",
                    "status",
                    "operation",
                    "job_id",
                    "progress",
                    "take_count",
                    "active_take_count",
                    "created_at",
                    "updated_at",
                    "assembly",
                )
                if key in item
            }
        )
        if len(items) >= arguments.limit:
            break
    return {"schema_version": "1", "session_id": arguments.session_id, "items": items}


def plan_export_variant(
    runtime: McpRuntime,
    arguments: PlanExportVariantInput,
) -> dict[str, Any]:
    output: dict[str, Any] = {
        "export_mode": arguments.export_mode,
        "audio_mode": arguments.audio_mode,
        "subtitle_mode": arguments.subtitle_mode,
        "subtitle_selection": arguments.subtitle_selection,
        "subtitle_format": arguments.subtitle_format,
    }
    if arguments.generation_run_id:
        output["generation_run_id"] = arguments.generation_run_id
    return runtime.require_application().create_workflow_plan(
        arguments.session_id,
        target_stage="export",
        overrides={"output": output},
        expires_in_minutes=arguments.expires_in_minutes,
    )


def _safe_filename(value: str) -> str:
    name = Path(value).name
    name = re.sub(r"[^A-Za-z0-9._() -]+", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        raise PandratorMcpError("validation_error", "The local output filename is invalid.")
    return name[:255]


def download_artifact(
    runtime: McpRuntime,
    arguments: DownloadArtifactInput,
) -> dict[str, Any]:
    profile = runtime.profile
    output_root = profile.local_output_root if profile else None
    if (
        not output_root
        and profile
        and profile.mode == TargetMode.LOCAL_MANAGED
        and profile.workspace
    ):
        output_root = str(Path(profile.workspace) / "exports")
    if not output_root:
        raise PandratorMcpError(
            "precondition_required",
            "Configure a local output root before materializing artifacts.",
        )
    application = runtime.require_application()
    context = application.artifact_context(arguments.artifact_id)
    artifact = context.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("state") == "deleted":
        raise PandratorMcpError("not_found", "The requested artifact is unavailable.")
    expected_size = int(artifact.get("size_bytes") or 0)
    if expected_size <= 0:
        raise PandratorMcpError(
            "source_unavailable",
            "The requested artifact has no materialized content to download.",
        )
    root = Path(output_root).resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    source_name = str(
        arguments.filename
        or (artifact.get("metadata_json") or {}).get("original_filename")
        or Path(str(artifact.get("relative_path") or "")).name
        or f"artifact-{arguments.artifact_id[:8]}"
    )
    destination = (root / _safe_filename(source_name)).resolve(strict=False)
    try:
        destination.relative_to(root)
    except ValueError as error:
        raise PandratorMcpError(
            "network_policy_denied",
            "The local output filename escapes the configured output root.",
        ) from error
    result = application.download_artifact(
        arguments.artifact_id,
        destination,
        expected_size=expected_size,
        expected_hash=(str(artifact.get("content_hash")) if artifact.get("content_hash") else None),
    )
    return {
        "schema_version": "1",
        "artifact_id": arguments.artifact_id,
        **result,
    }
