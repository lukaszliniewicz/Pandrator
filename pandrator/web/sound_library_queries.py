"""Bounded read projections for the global sound library."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, aliased

from .models import Job, SoundItem, SoundVariant

MAX_PAGE_SIZE = 100
MAX_QUERY_LENGTH = 200
_COMPACT_PROVENANCE = (
    "provider",
    "model_id",
    "operation",
    "kind",
    "prompt",
    "seed",
    "backend",
    "duration_seconds",
)
_RUNTIME_IDENTITY_FIELDS = (
    "model_sha256", "model_revision", "runtime_version", "runtime_sha256",
    "minimum_runtime_version",
)
_HEX_SHA256 = re.compile(r"^[a-fA-F0-9]{64}$")
_SENSITIVE_PARAMETER_PARTS = (
    "path",
    "directory",
    "api_key",
    "apikey",
    "authorization",
    "auth_token",
    "access_token",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
)
_TEXTUAL_PARAMETER_KEYS = frozenset(
    {"negative_prompt", "tags", "keyscale", "timesignature", "language", "sampler", "sampler_mode"}
)
_NATIVE_FILE_SUFFIXES = frozenset(
    {".bin", ".ckpt", ".gguf", ".json", ".model", ".onnx", ".pt", ".pth", ".safetensors", ".wav"}
)


def _created(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _created_text(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _page_arguments(cursor: str | None, limit: int) -> tuple[datetime | None, str | None, int]:
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
        raise ValueError(f"limit must be an integer between 1 and {MAX_PAGE_SIZE}.")
    if cursor is None:
        return None, None, limit
    if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
        raise ValueError("Invalid sound-library cursor.")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        parsed = json.loads(raw)
        created_at = datetime.fromisoformat(parsed["created_at"])
        record_id = parsed["id"]
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"created_at", "id"}
            or not isinstance(record_id, str)
            or not record_id
            or len(record_id) > 36
        ):
            raise ValueError
        return _created(created_at), record_id, limit
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, binascii.Error) as error:
        raise ValueError("Invalid sound-library cursor.") from error


def _encode_cursor(created_at: datetime, record_id: str) -> str:
    payload = json.dumps(
        {"created_at": _created(created_at).isoformat(), "id": record_id},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _apply_page(query, model, created_at: datetime | None, record_id: str | None):
    if created_at is not None and record_id is not None:
        query = query.where(
            or_(
                model.created_at < created_at,
                and_(model.created_at == created_at, model.id < record_id),
            )
        )
    return query.order_by(model.created_at.desc(), model.id.desc())


def _job_status(state: str, job_status: str | None) -> str:
    if state != "queued":
        return state
    if job_status in {"queued", "running", "cancel_requested"}:
        return "running" if job_status != "queued" else "queued"
    if job_status in {"canceled", "interrupted"}:
        return "canceled"
    return "failed"


def _looks_like_native_path(value: str) -> bool:
    if (
        value.startswith(("/", "\\", "~"))
        or "\\" in value
        or "://" in value
        or re.match(r"^[A-Za-z]:[\\/]", value)
        or ".." in PurePosixPath(value).parts
    ):
        return True
    return PurePosixPath(value).suffix.casefold() in _NATIVE_FILE_SUFFIXES


def _looks_like_path(value: str) -> bool:
    return _looks_like_native_path(value) or len(PurePosixPath(value).parts) > 1


def _sensitive_parameter_key(value: str) -> bool:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value).casefold()
    tokens = set(re.split(r"[^a-z0-9]+", normalized))
    return bool(tokens & {"file", "filename", "dir"}) or any(
        part in normalized for part in _SENSITIVE_PARAMETER_PARTS
    )


def _safe_parameters(value: Any) -> dict[str, str | bool | int | float] | None:
    if not isinstance(value, dict) or len(value) > 32:
        return None
    result: dict[str, str | bool | int | float] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 64 or _sensitive_parameter_key(key):
            continue
        if isinstance(item, str):
            safe_text = (
                key.casefold() in _TEXTUAL_PARAMETER_KEYS
                and not _looks_like_native_path(item)
            )
            if (
                len(item) <= 2000
                and not any(ord(character) < 32 for character in item)
                and (safe_text or not _looks_like_path(item))
            ):
                result[key] = item
        elif type(item) is bool:
            result[key] = item
        elif type(item) is int and abs(item) <= 2**63 - 1:
            result[key] = item
        elif type(item) is float and math.isfinite(item):
            result[key] = item
        if len(result) == 32:
            break
    return result


def _safe_asset_manifest(
    value: Any,
    *,
    from_model_assets: bool = False,
    default_revision: Any = None,
) -> list[dict[str, str | int]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, str | int]] = []
    for raw in value[:64]:
        if not isinstance(raw, dict):
            continue
        filename = raw.get("path" if from_model_assets else "filename")
        digest = raw.get("sha256")
        if (
            not isinstance(filename, str)
            or len(filename) > 255
            or filename.startswith(("/", "\\", "~"))
            or "\\" in filename
            or "://" in filename
            or re.match(r"^[A-Za-z]:", filename)
            or any(ord(character) < 32 for character in filename)
            or any(part in {"", ".", ".."} for part in filename.split("/"))
            or not isinstance(digest, str)
            or not _HEX_SHA256.fullmatch(digest)
        ):
            continue
        asset: dict[str, str | int] = {"filename": filename, "sha256": digest.lower()}
        revision = raw.get("revision")
        if revision is None and from_model_assets:
            revision = default_revision
        if (
            isinstance(revision, str)
            and len(revision) <= 255
            and not _looks_like_path(revision)
            and not any(ord(character) < 32 for character in revision)
        ):
            asset["revision"] = revision
        size = raw.get("size_bytes" if from_model_assets else "bytes")
        if type(size) is int and 0 <= size <= 2**63 - 1:
            asset["bytes"] = size
        result.append(asset)
    return result


def _safe_runtime_identity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    safe: dict[str, Any] = {
        key: item[:255]
        for key in _RUNTIME_IDENTITY_FIELDS
        if isinstance((item := value.get(key)), str)
        and len(item) <= 255
        and not _looks_like_path(item)
        and not any(ord(character) < 32 for character in item)
    }
    manifest = _safe_asset_manifest(
        value.get("model_assets"),
        from_model_assets=True,
        default_revision=value.get("model_assets_revision"),
    )
    if not manifest:
        manifest = _safe_asset_manifest(value.get("asset_manifest"))
    if manifest:
        safe["asset_manifest"] = manifest
    return safe or None


def _provenance(value: Any, *, include_detail: bool = False) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    result = {key: source[key] for key in _COMPACT_PROVENANCE if key in source}
    kind = result.get("kind")
    if not isinstance(kind, str) or kind not in {"sound", "music"}:
        result.pop("kind", None)
    if include_detail:
        lyrics = source.get("lyrics")
        if isinstance(lyrics, str):
            result["lyrics"] = lyrics[:16000]
        parameters = _safe_parameters(source.get("parameters"))
        if parameters:
            result["parameters"] = parameters
        for key in ("threads", "num_inference_steps"):
            item = source.get(key)
            if type(item) is int and item >= 1:
                result[key] = item
        guidance = source.get("guidance_scale")
        if isinstance(guidance, (int, float)) and not isinstance(guidance, bool) and math.isfinite(guidance) and guidance >= 0:
            result["guidance_scale"] = guidance
        identity = _safe_runtime_identity(source.get("runtime_identity"))
        if identity:
            result["runtime_identity"] = identity
    return result


def candidate_summary(
    variant: SoundVariant, job_status: str | None = None, *, include_detail: bool = False
) -> dict[str, Any]:
    return {
        "id": variant.id,
        "status": _job_status(variant.state, job_status),
        "job_id": variant.job_id,
        "artifact_id": variant.artifact_id,
        "item_id": variant.item_id,
        "duration_seconds": variant.duration_seconds,
        "sample_rate": variant.sample_rate,
        "channels": variant.channels,
        "created_at": _created_text(variant.created_at),
        "provenance": _provenance(variant.provenance_json, include_detail=include_detail),
    }


def _item_summary(
    item: SoundItem,
    preferred: SoundVariant | None,
    preferred_job_status: str | None,
    *,
    include_detail: bool = False,
) -> dict[str, Any]:
    return {
        "id": item.id,
        "name": item.name,
        "tags": list(item.tags_json or []),
        "revision": item.revision,
        "archived": bool(item.archived),
        "preferred_variant": (
            candidate_summary(preferred, preferred_job_status, include_detail=include_detail)
            if preferred else None
        ),
        "created_at": _created_text(item.created_at),
        "updated_at": _created_text(item.updated_at),
    }


def _preferred_joins():
    preferred = aliased(SoundVariant)
    preferred_job = aliased(Job)
    return preferred, preferred_job


def _sounds_query():
    preferred, preferred_job = _preferred_joins()
    query = (
        select(SoundItem, preferred, preferred_job.status)
        .outerjoin(
            preferred,
            and_(
                preferred.id == SoundItem.preferred_variant_id,
                preferred.item_id == SoundItem.id,
            ),
        )
        .outerjoin(preferred_job, preferred_job.id == preferred.job_id)
    )
    return query, preferred


def _validate_query(query: str, tag: str | None, archived: bool) -> tuple[str, str | None]:
    if not isinstance(query, str) or len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"query must be a string of at most {MAX_QUERY_LENGTH} characters.")
    if tag is not None and (not isinstance(tag, str) or len(tag) > 64):
        raise ValueError("tag must be a string of at most 64 characters.")
    if type(archived) is not bool:
        raise ValueError("archived must be a boolean.")
    return query.strip(), tag


def _name_or_tag_match(query: str):
    escaped = query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    tag_values = func.json_each(SoundItem.tags_json).table_valued("value").alias("sound_tag_search")
    tag_match = (
        select(1)
        .select_from(tag_values)
        .where(func.lower(tag_values.c.value).like(pattern, escape="\\"))
        .correlate(SoundItem)
        .exists()
    )
    return or_(func.lower(SoundItem.name).like(pattern, escape="\\"), tag_match)


def _exact_tag_match(tag: str):
    tag_values = func.json_each(SoundItem.tags_json).table_valued("value").alias("sound_tag_exact")
    return (
        select(1)
        .select_from(tag_values)
        .where(tag_values.c.value == tag)
        .correlate(SoundItem)
        .exists()
    )


def _page_result(rows: list[Any], limit: int, row_id) -> dict[str, Any]:
    visible = rows[:limit]
    next_cursor = None
    if len(rows) > limit and visible:
        last = visible[-1]
        next_cursor = _encode_cursor(last[0].created_at, row_id(last))
    return {"items": visible, "next_cursor": next_cursor}


def list_sounds(
    session: Session,
    *,
    query: str = "",
    tag: str | None = None,
    cursor: str | None = None,
    limit: int = 40,
    archived: bool = False,
) -> dict[str, Any]:
    query, tag = _validate_query(query, tag, archived)
    cursor_at, cursor_id, page_size = _page_arguments(cursor, limit)
    statement, _preferred = _sounds_query()
    statement = statement.where(SoundItem.archived.is_(archived))
    if query:
        statement = statement.where(_name_or_tag_match(query))
    if tag is not None:
        statement = statement.where(_exact_tag_match(tag))
    statement = _apply_page(statement, SoundItem, cursor_at, cursor_id).limit(page_size + 1)
    rows = list(session.execute(statement).all())
    page = _page_result(rows, page_size, lambda row: row[0].id)
    page["items"] = [
        _item_summary(item, preferred, job_status)
        for item, preferred, job_status in page["items"]
    ]
    return page


def get_sound(
    session: Session,
    item_id: str,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    cursor_at, cursor_id, page_size = _page_arguments(cursor, limit)
    item_statement, _preferred = _sounds_query()
    row = session.execute(item_statement.where(SoundItem.id == item_id)).first()
    if row is None:
        raise KeyError(item_id)
    item, preferred, preferred_job_status = row

    statement = (
        select(SoundVariant, Job.status)
        .outerjoin(Job, Job.id == SoundVariant.job_id)
        .where(SoundVariant.item_id == item_id)
    )
    statement = _apply_page(statement, SoundVariant, cursor_at, cursor_id).limit(page_size + 1)
    rows = list(session.execute(statement).all())
    page = _page_result(rows, page_size, lambda result: result[0].id)
    return {
        **_item_summary(item, preferred, preferred_job_status, include_detail=True),
        "variants": {
            "items": [
                candidate_summary(variant, status, include_detail=True)
                for variant, status in page["items"]
            ],
            "next_cursor": page["next_cursor"],
        },
    }


def list_candidates(
    session: Session,
    *,
    cursor: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    cursor_at, cursor_id, page_size = _page_arguments(cursor, limit)
    statement = select(SoundVariant, Job.status).outerjoin(Job, Job.id == SoundVariant.job_id)
    statement = _apply_page(statement, SoundVariant, cursor_at, cursor_id).limit(page_size + 1)
    rows = list(session.execute(statement).all())
    page = _page_result(rows, page_size, lambda result: result[0].id)
    page["items"] = [candidate_summary(variant, status) for variant, status in page["items"]]
    return page


def get_candidate(session: Session, candidate_id: str) -> dict[str, Any]:
    row = session.execute(
        select(SoundVariant, Job.status)
        .outerjoin(Job, Job.id == SoundVariant.job_id)
        .where(SoundVariant.id == candidate_id)
    ).first()
    if row is None:
        raise KeyError(candidate_id)
    return candidate_summary(row[0], row[1], include_detail=True)
