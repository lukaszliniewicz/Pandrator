"""Forward-only importer from the Qt-era database and portable JSON files."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database, upgrade_database
from .models import (
    AppSetting,
    Document,
    DocumentRevision,
    Provider,
    ProviderModel,
    Segment,
    SessionRecord,
    new_id,
)


MIGRATION_VERSION = 1
_SECRET_KEYS = {"api_key", "token", "password", "secret", "access_token", "refresh_token"}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _redact_mapping(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (None if key.lower() in _SECRET_KEYS else _redact_mapping(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_mapping(item) for item in value]
    return value


def _legacy_settings(paths: DataPaths) -> dict[str, Any]:
    if paths.legacy_database.is_file():
        try:
            connection = sqlite3.connect(paths.legacy_database)
            row = connection.execute("SELECT payload_json FROM app_settings_current WHERE singleton_id = 1").fetchone()
            connection.close()
            if row:
                payload = json.loads(row[0])
                if isinstance(payload, dict):
                    return payload
        except (sqlite3.DatabaseError, json.JSONDecodeError):
            pass
    payload = _load_json(paths.root / "global_settings.json")
    if isinstance(payload, dict) and isinstance(payload.get("settings"), dict):
        return payload["settings"]
    return payload if isinstance(payload, dict) else {}


def _legacy_session_rows(paths: DataPaths) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if paths.legacy_database.is_file():
        try:
            connection = sqlite3.connect(paths.legacy_database)
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute("SELECT * FROM sessions WHERE trashed_at IS NULL")]
            connection.close()
        except sqlite3.DatabaseError:
            rows = []
    known = {str(row.get("session_name")) for row in rows}
    if paths.legacy_outputs.is_dir():
        for child in paths.legacy_outputs.iterdir():
            if child.is_dir() and child.name != ".trash" and child.name not in known:
                rows.append({"session_name": child.name, "session_path": str(child), "status": "idle"})
    return rows


def create_metadata_backup(paths: DataPaths) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = paths.backups / f"pre-web-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    candidates = [paths.legacy_database, paths.root / "global_settings.json", paths.root / "config.json"]
    for candidate in candidates:
        if candidate.is_file():
            shutil.copy2(candidate, backup / candidate.name)
    if paths.legacy_outputs.is_dir():
        for config in paths.legacy_outputs.glob("*/session_config.json"):
            target = backup / "sessions" / config.parent.name / config.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config, target)
    return backup


def _import_providers(database: Database, settings: dict[str, Any]) -> int:
    llm = settings.get("llm") if isinstance(settings.get("llm"), dict) else {}
    configs = llm.get("provider_configs") if isinstance(llm, dict) else []
    default_model = str(llm.get("default_model") or "") if isinstance(llm, dict) else ""
    count = 0
    with database.session() as session:
        for raw in configs if isinstance(configs, list) else []:
            if not isinstance(raw, dict):
                continue
            provider_key = str(raw.get("provider") or "custom")
            provider_label = str(raw.get("name") or raw.get("label") or raw.get("id") or provider_key)
            provider = Provider(
                kind="llm",
                provider_key=provider_key,
                label=provider_label,
                enabled=bool(raw.get("enabled", True)),
                base_url=str(raw.get("base_url") or raw.get("api_base") or "") or None,
                secret_ref=str(raw.get("secret_ref") or "") or None,
                options_json=_redact_mapping({key: value for key, value in raw.items() if key != "models"}),
            )
            session.add(provider)
            session.flush()
            for model_raw in raw.get("models", []):
                model = {"id": model_raw} if isinstance(model_raw, str) else model_raw
                if not isinstance(model, dict) or not str(model.get("id") or "").strip():
                    continue
                model_id = str(model["id"]).strip()
                canonical = f"{provider_key}/{model_id}"
                session.add(
                    ProviderModel(
                        provider_id=provider.id,
                        model_id=model_id,
                        is_default=default_model in {model_id, canonical},
                        default_temperature=model.get("default_temperature"),
                        default_reasoning_effort=str(model.get("default_reasoning_effort") or "") or None,
                        input_cost_per_million=model.get("input_cost_per_million"),
                        cached_input_cost_per_million=model.get("cached_input_cost_per_million"),
                        output_cost_per_million=model.get("output_cost_per_million"),
                    )
                )
            count += 1
    return count


def _sentence_text(item: dict[str, Any]) -> str:
    for key in ("processed_sentence", "sentence", "original_sentence", "text"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _import_sentences(database: Database, legacy_path: Path, record: SessionRecord) -> int:
    candidates = list(legacy_path.glob("*_sentences.json"))
    if not candidates:
        return 0
    payload = _load_json(candidates[0])
    if not isinstance(payload, list):
        return 0
    normalized = [item for item in payload if isinstance(item, dict) and _sentence_text(item)]
    if not normalized:
        return 0
    digest = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    with database.session() as session:
        document = Document(session_id=record.id, stage="legacy_text")
        session.add(document)
        session.flush()
        revision = DocumentRevision(document_id=document.id, revision_number=1, content_hash=digest, reviewed=True)
        session.add(revision)
        session.flush()
        for index, item in enumerate(normalized):
            session.add(
                Segment(
                    revision_id=revision.id,
                    ordinal=index,
                    start_ms=item.get("start_ms"),
                    end_ms=item.get("end_ms"),
                    text=_sentence_text(item),
                    speaker=str(item.get("speaker") or "") or None,
                    metadata_json=_redact_mapping(item),
                )
            )
        document.active_revision_id = revision.id
    return len(normalized)


def import_legacy_data(paths: DataPaths) -> dict[str, Any]:
    paths.ensure()
    if paths.migration_marker.is_file():
        upgrade_database(paths.database)
        return _load_json(paths.migration_marker) or {"version": MIGRATION_VERSION, "status": "complete"}

    backup = create_metadata_backup(paths)
    if paths.database.exists():
        incomplete = paths.database.with_name(f"{paths.database.name}.incomplete-{new_id()}")
        paths.database.replace(incomplete)

    try:
        upgrade_database(paths.database)
        database = Database(paths.database)
        settings = _legacy_settings(paths)
        with database.session() as session:
            session.add(AppSetting(key="global", value_json=_redact_mapping(settings)))
        provider_count = _import_providers(database, settings)
        artifact_service = ArtifactService(database, paths)
        imported_sessions = 0
        imported_segments = 0
        imported_artifacts = 0
        for row in _legacy_session_rows(paths):
            name = str(row.get("session_name") or "Untitled Session")
            legacy_path = Path(str(row.get("session_path") or paths.legacy_outputs / name)).resolve()
            config = _load_json(legacy_path / "session_config.json")
            state = config.get("state") if isinstance(config, dict) and isinstance(config.get("state"), dict) else {}
            workflow = state.get("workflow") if isinstance(state.get("workflow"), dict) else {}
            record = SessionRecord(
                name=name,
                legacy_name=name,
                legacy_path=str(legacy_path),
                workflow_kind=str(workflow.get("workflow_kind") or ("voiceover" if row.get("dubbing_mode") else "audiobook")),
                workflow_preset=str(workflow.get("workflow_preset") or "custom"),
                included_stages_json=list(workflow.get("included_stages") or []),
                status=str(row.get("status") or "idle"),
            )
            with database.session() as session:
                session.add(record)
                session.flush()
                session.expunge(record)
            imported_sessions += 1
            if legacy_path.is_dir() and paths.root in legacy_path.parents:
                imported_segments += _import_sentences(database, legacy_path, record)
                for file_path in legacy_path.rglob("*"):
                    if not file_path.is_file() or file_path.name in {"session_config.json", "metadata.json"}:
                        continue
                    try:
                        artifact_service.register(
                            file_path,
                            kind=file_path.suffix.lower().lstrip(".") or "file",
                            role="legacy",
                            session_id=record.id,
                            calculate_hash=False,
                            metadata={"legacy": True},
                        )
                        imported_artifacts += 1
                    except (OSError, ValueError):
                        continue

        with database.session() as session:
            validated_sessions = session.scalar(select(func.count()).select_from(SessionRecord)) or 0
        database.dispose()
        if validated_sessions != imported_sessions:
            raise RuntimeError(
                f"Legacy migration validation failed: imported {imported_sessions}, database has {validated_sessions}."
            )
        result = {
            "version": MIGRATION_VERSION,
            "status": "complete",
            "database": paths.database.name,
            "backup": str(backup.relative_to(paths.root)),
            "sessions": imported_sessions,
            "segments": imported_segments,
            "artifacts": imported_artifacts,
            "providers": provider_count,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_json(paths.migration_marker, result)
        return result
    except Exception:
        if paths.database.exists():
            paths.database.unlink()
        raise

