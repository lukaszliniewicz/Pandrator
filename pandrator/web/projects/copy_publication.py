"""Persist one verified copy graph in the caller's freshly authorized writer.

This is a persistence leaf, not an authority or replay entry point. The caller
owns current source/file/destination authorization, the immediate transaction,
its durable result/audit, commit/rollback and staged-file lifetime.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, cast

from sqlalchemy import JSON, Boolean, Float, Integer, String, Table, bindparam, select, update
from sqlalchemy.orm import Session

from .. import models as m
from ..knowledge import KnowledgeLedgerStore, KnowledgeValidationError
from ..recorded_speech_contracts import PROVENANCE_KEY
from ..settings_policy import SETTING_SECTIONS, _secret_free
from ..sound_project_models import ProjectSoundCue, ProjectSoundPlan
from .copy_audio import (
    TAKE_FIELDS,
    remap_audio_metadata,
    remap_audio_rows,
    validate_audio_copy_graph,
)
from .copy_connections import (
    connection_key,
    validate_connection_state,
    validate_current_connections,
)
from .copy_contracts import (
    SUPPORTED_COPY_STATE_VERSIONS,
    CopyFileSource,
    ProjectCopyError,
    ProjectCopySnapshot,
)
from .copy_evidence import remap_evidence, validate_evidence_graph
from .copy_files import StagedCopyFile, StagedProjectFiles
from .copy_media import rebind_published_metadata, remap_revision, validate_media_graph
from .copy_outputs import COPIED_OUTPUT_KEY, SAVED_SPEECH_ROLE, validate_output_copy_graph
from .copy_performance import (
    PERFORMANCE_FAMILIES,
    remap_performance_plan_rows,
    validate_performance_copy_graph,
)
from .copy_performance import (
    PLAN_FIELDS as PERFORMANCE_PLAN_FIELDS,
)
from .copy_sound import (
    CUE_FIELDS as SOUND_CUE_FIELDS,
)
from .copy_sound import (
    PLAN_FIELDS as SOUND_PLAN_FIELDS,
)
from .copy_sound import SOUND_FAMILIES, remap_sound_copy_rows, validate_sound_copy_graph
from .copy_sound import (
    VARIANT_FIELDS as SOUND_VARIANT_FIELDS,
)
from .copy_speech import (
    freeze_generation_controls,
    remap_cast_reference,
    remap_segment_reference,
    remap_tts_reference,
    validate_speech_copy_graph,
)
from .copy_speech_plan import (
    PLAN_FIELDS as SPEECH_PLAN_FIELDS,
)
from .copy_speech_plan import (
    REVIEW_FIELDS as SPEECH_REVIEW_FIELDS,
)
from .copy_speech_plan import (
    REVISION_FIELDS as SPEECH_REVISION_FIELDS,
)
from .copy_speech_plan import (
    SEGMENT_FIELDS as SPEECH_SEGMENT_FIELDS,
)
from .copy_speech_plan import (
    SPEECH_FAMILIES,
    remap_speech_plan_rows,
    validate_speech_plan_graph,
)

_CHUNK = 500
_HASH = re.compile(r"[0-9a-fA-F]{64}\Z", re.ASCII)
_SUFFIX = re.compile(r"\.[A-Za-z0-9]{1,16}\Z", re.ASCII)
_FIELDS = {
    "audio_takes": " ".join(sorted(TAKE_FIELDS)),
    "projects": "id workspace_id source_session_id name created_by_user_id",
    "sessions": "id project_id name workflow_kind interface_mode source_language target_language workflow_preset included_stages_json",
    "translation_projects": "id project_root_id name source_session_id checkpoint_artifact_id source_content_hash source_language source_media_edit_revision_id source_media_edit_content_hash",
    "translation_project_branches": "id project_id session_id target_language source_checkpoint_artifact_id source_content_hash",
    "session_stage_selections": "session_id stage_key artifact_id revision",
    "session_sources": "id session_id source_asset_id role is_current revision",
    "source_assets": "id workspace_id artifact_id display_name kind mime_type external_path size_bytes content_hash state metadata_json revision",
    "sources": "id session_id kind display_name artifact_id external_path content_hash metadata_json",
    "artifacts": "id workspace_id project_id session_id kind role relative_path mime_type size_bytes content_hash state settings_hash metadata_json",
    "artifact_edges": "parent_artifact_id child_artifact_id relation",
    "outcome_plans": "session_id value_json revision",
    "documents": "id session_id stage language active_revision_id",
    "document_revisions": "id document_id parent_revision_id revision_number content_hash reviewed settings_hash",
    "segments": "id revision_id ordinal node_kind start_ms end_ms text speaker metadata_json",
    "timed_words": "id revision_id segment_id ordinal text start_ms end_ms speaker confidence metadata_json",
    "segment_lineage": "parent_segment_id child_segment_id relation sequence",
    "session_settings": "session_id section value_json",
    "project_speech_references": (
        "session_id slot_key selector_json selector_hash binding_json "
        "input_artifact_id input_content_hash revision"
    ),
    "knowledge_ledgers": "id session_id kind source_language target_language payload_json",
    "subtitle_evidence": (
        "id session_id source_artifact_id source_media_artifact_id source_revision_id "
        "source_segment_id cue_id start_ms end_ms clip_start_ms clip_end_ms reason "
        "routes_json status clip_artifact_id candidates_json resolution_json"
    ),
    "media_edit_plans": "id session_id active_revision_id",
    "media_edit_plan_revisions": (
        "id plan_id parent_revision_id revision_number source_media_artifact_id "
        "editorial_transcript_artifact_id timing_artifact_id duration_ms instructions "
        "keep_ranges_json cues_json evidence_json operation_json reviewed content_hash"
    ),
}
_FIELDS.update({"generation_plans": " ".join(sorted(SPEECH_PLAN_FIELDS)),
                "generation_plan_revisions": " ".join(sorted(SPEECH_REVISION_FIELDS)),
                "generation_segments": " ".join(sorted(SPEECH_SEGMENT_FIELDS)),
                "speech_plan_reviews": " ".join(sorted(SPEECH_REVIEW_FIELDS))})
_FIELDS.update({"project_sound_plans": " ".join(sorted(SOUND_PLAN_FIELDS)),
                "project_sound_cues": " ".join(sorted(SOUND_CUE_FIELDS)),
                "sound_variants": " ".join(sorted(SOUND_VARIANT_FIELDS))})
_FIELDS["performance_plans"] = " ".join(sorted(PERFORMANCE_PLAN_FIELDS))

_MODELS = {
    "performance_plans": m.PerformancePlan,
    "project_sound_plans": ProjectSoundPlan,
    "project_sound_cues": ProjectSoundCue,
    "sound_variants": m.SoundVariant,
    "generation_plans": m.GenerationPlan,
    "generation_plan_revisions": m.GenerationPlanRevision,
    "generation_segments": m.GenerationSegment,
    "speech_plan_reviews": m.SpeechPlanReview,
    "audio_takes": m.AudioTake,
    "projects": m.Project,
    "sessions": m.SessionRecord,
    "translation_projects": m.TranslationProject,
    "translation_project_branches": m.TranslationProjectBranch,
    "session_stage_selections": m.SessionStageSelection,
    "session_sources": m.SessionSource,
    "source_assets": m.SourceAsset,
    "sources": m.SourceRecord,
    "artifacts": m.Artifact,
    "artifact_edges": m.ArtifactEdge,
    "outcome_plans": m.OutcomePlan,
    "documents": m.Document,
    "document_revisions": m.DocumentRevision,
    "segments": m.Segment,
    "timed_words": m.TimedWord,
    "segment_lineage": m.SegmentLineage,
    "session_settings": m.SessionSetting,
    "project_speech_references": m.ProjectSpeechReference,
    "knowledge_ledgers": m.KnowledgeLedger,
    "subtitle_evidence": m.SubtitleEvidence,
    "media_edit_plans": m.MediaEditPlan,
    "media_edit_plan_revisions": m.MediaEditPlanRevision,
}
_TABLES = {name: cast(Table, model.__table__) for name, model in _MODELS.items()}
_PRIMARY = {
    "session_stage_selections": ("session_id", "stage_key"),
    "session_settings": ("session_id", "section"),
    "project_speech_references": ("session_id", "slot_key"),
    "outcome_plans": ("session_id",),
    "speech_plan_reviews": ("revision_id",),
    "artifact_edges": ("parent_artifact_id", "child_artifact_id"),
    "segment_lineage": ("parent_segment_id", "child_segment_id"),
}
_ID_TABLES = frozenset(set(_FIELDS) - set(_PRIMARY))
_REFERENCE_FAMILIES = re.compile(
    r"(?:^|_)(?:artifact|session|document|revision|segment|source_asset|evidence|plan)(?:_|$)"
)


def _invalid() -> ProjectCopyError:
    return ProjectCopyError(
        "copy_graph_invalid", "The saved project state is incomplete or inconsistent."
    )


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value)


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(value)
        return type(value) is str
    except (ValueError, AttributeError, TypeError):
        return False


def copy_identity(copy_id: str, table: str, source_id: str) -> str:
    if (
        not _uuid(copy_id)
        or type(table) is not str
        or table not in _FIELDS
        or not _identifier(source_id)
    ):
        raise _invalid()
    result = str(uuid.uuid5(uuid.UUID(copy_id), table + ":" + source_id))
    if result == source_id:
        raise _invalid()
    return result


@dataclass(frozen=True, slots=True)
class ProjectCopyResult:
    project_id: str
    source_session_id: str
    translation_project_id: str | None
    workspace_id: str
    name: str
    fingerprint: str

    def manifest(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_session_id": self.source_session_id,
            "translation_project_id": self.translation_project_id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "fingerprint": self.fingerprint,
        }


def _chunks(values: Iterable[Any]):
    items = list(values)
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


def _row_types(table: str, row: dict[str, Any]) -> None:
    columns = _MODELS[table].__table__.c
    for key, value in row.items():
        column = columns[key]
        if value is None:
            if not column.nullable:
                raise _invalid()
        elif isinstance(column.type, Boolean):
            if type(value) is not bool:
                raise _invalid()
        elif isinstance(column.type, Integer):
            if type(value) is not int:
                raise _invalid()
        elif isinstance(column.type, Float):
            if type(value) not in (float, int):
                raise _invalid()
        elif isinstance(column.type, String):
            if type(value) is not str:
                raise _invalid()
        elif isinstance(column.type, JSON):
            if table == "generation_segments" and key == "source_segment_ids_json":
                if type(value) is not list or any(type(item) not in (int, str) for item in value):
                    raise _invalid()
            elif key == "included_stages_json":
                if type(value) is not list or any(type(item) is not str for item in value):
                    raise _invalid()
            elif key == "routes_json":
                if type(value) is not list or any(type(item) is not str for item in value):
                    raise _invalid()
            elif key == "candidates_json":
                if type(value) is not list or any(type(item) is not dict for item in value):
                    raise _invalid()
            elif table == "performance_plans" and key == "units_json":
                if type(value) is not list or any(type(item) is not dict for item in value):
                    raise _invalid()
            elif table == "media_edit_plan_revisions" and key in {
                "keep_ranges_json",
                "cues_json",
            }:
                if type(value) is not list or any(type(item) is not dict for item in value):
                    raise _invalid()
            elif type(value) is not dict:
                raise _invalid()


def _decode(snapshot: ProjectCopySnapshot):
    if (
        type(snapshot) is not ProjectCopySnapshot
        or type(snapshot.blockers) is not tuple
        or not snapshot.ready
    ):
        raise _invalid()
    if type(snapshot.state_json) is not str or type(snapshot.fingerprint) is not str:
        raise _invalid()
    try:
        state = json.loads(snapshot.state_json)
        canonical = json.dumps(
            state, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        digest = hashlib.sha256(snapshot.state_json.encode("utf-8")).hexdigest()
    except (ValueError, TypeError, UnicodeError) as error:
        raise _invalid() from error
    if (
        canonical != snapshot.state_json
        or digest != snapshot.fingerprint
        or type(state) is not dict
    ):
        raise _invalid()
    if (
        type(state["schema_version"]) is not int
        or state["schema_version"] not in SUPPORTED_COPY_STATE_VERSIONS
    ):
        raise _invalid()
    if state["binding_fields"] != {} or state["unsupported_counts"] != {}:
        raise _invalid()
    rows = state["rows"]
    if type(rows) is not dict:
        raise _invalid()
    if state["schema_version"] < 15:
        if PERFORMANCE_FAMILIES & rows.keys():
            raise _invalid()
        rows = {**rows, **{key: [] for key in PERFORMANCE_FAMILIES}}
    if state["schema_version"] < 14:
        if SOUND_FAMILIES & rows.keys():
            raise _invalid()
        rows = {**rows, **{key: [] for key in SOUND_FAMILIES}}
    if state["schema_version"] < 9:
        if "audio_takes" in rows or any(PROVENANCE_KEY in row.get("metadata_json", {}) for row in rows.get("artifacts", [])):
            raise _invalid()
        rows = {**rows, "audio_takes": []}
    if state["schema_version"] < 8:
        if SPEECH_FAMILIES & rows.keys():
            raise _invalid()
        rows = {**rows, **{key: [] for key in SPEECH_FAMILIES}}
    if state["schema_version"] < 5:
        if "project_speech_references" in rows:
            raise _invalid()
        rows = {**rows, "project_speech_references": []}
    if state["schema_version"] == 1:
        legacy_fields = set(_FIELDS) - {
            "knowledge_ledgers",
            "subtitle_evidence",
            "media_edit_plans",
            "media_edit_plan_revisions",
        }
        if set(rows) != legacy_fields:
            raise _invalid()
        rows = {
            **rows,
            "knowledge_ledgers": [],
            "subtitle_evidence": [],
            "media_edit_plans": [],
            "media_edit_plan_revisions": [],
        }
    elif state["schema_version"] == 2:
        legacy_fields = set(_FIELDS) - {
            "subtitle_evidence",
            "media_edit_plans",
            "media_edit_plan_revisions",
        }
        if set(rows) != legacy_fields:
            raise _invalid()
        rows = {
            **rows,
            "subtitle_evidence": [],
            "media_edit_plans": [],
            "media_edit_plan_revisions": [],
        }
    elif state["schema_version"] == 3:
        legacy_fields = set(_FIELDS) - {"media_edit_plans", "media_edit_plan_revisions"}
        if set(rows) != legacy_fields:
            raise _invalid()
        rows = {**rows, "media_edit_plans": [], "media_edit_plan_revisions": []}
    elif set(rows) != set(_FIELDS):
        raise _invalid()
    by_id: dict[str, dict[str, dict[str, Any]]] = {}
    all_ids: set[str] = set()
    for table, fields in _FIELDS.items():
        if type(rows[table]) is not list:
            raise _invalid()
        keys = set(fields.split())
        primary = _PRIMARY.get(table, ("id",))
        seen = set()
        by_id[table] = {}
        for row in rows[table]:
            if type(row) is not dict or set(row) != keys:
                raise _invalid()
            _row_types(table, row)
            identity = tuple(row[key] for key in primary)
            if any(not _identifier(value) for value in identity) or identity in seen:
                raise _invalid()
            seen.add(identity)
            if table in _ID_TABLES:
                old_id = row["id"]
                if old_id in all_ids:
                    raise _invalid()
                all_ids.add(old_id)
                by_id[table][old_id] = row
    if state["schema_version"] < 7 and (
        any(row["kind"] == "generation_controls" for row in rows["knowledge_ledgers"])
        or any(row["slot_key"] != "tts" for row in rows["project_speech_references"])
    ):
        raise _invalid()
    if state["schema_version"] < 12 and any(
        row["slot_key"].startswith("segment:") for row in rows["project_speech_references"]
    ):
        raise _invalid()
    if state["schema_version"] < 13 and any(
        type(row["metadata_json"].get(PROVENANCE_KEY)) is dict
        and row["metadata_json"][PROVENANCE_KEY].get("version") == 2
        for row in rows["artifacts"]
    ):
        raise _invalid()
    if state["schema_version"] < 10:
        if any(row["role"] == SAVED_SPEECH_ROLE or COPIED_OUTPUT_KEY in row["metadata_json"]
               for row in rows["artifacts"]):
            raise _invalid()
    else:
        try:
            validate_output_copy_graph(rows, allow_sound_mix=state["schema_version"] >= 14)
        except (ValueError, TypeError, KeyError) as error:
            raise _invalid() from error
    try:
        validate_sound_copy_graph(rows, allow_sounds=state["schema_version"] >= 14)
        validate_performance_copy_graph(rows, allow_performance=state["schema_version"] >= 15)
    except (ValueError, TypeError, KeyError) as error:
        raise _invalid() from error
    validate_connection_state(state, rows)
    return rows, by_id, all_ids


def _references(rows, by_id, snapshot, *, allow_null_inputs=False, allow_segment_references=False,
                allow_saved_selections=False):
    roots = rows["projects"]
    if len(roots) != 1 or roots[0]["id"] != snapshot.source_project_id:
        raise _invalid()
    root = roots[0]
    source = root["source_session_id"]
    sessions = by_id["sessions"]
    if source not in sessions or any(
        row["project_id"] != root["id"] or row["workflow_kind"] not in {"subtitles", "media_edit", "voiceover"}
        for row in sessions.values()
    ):
        raise _invalid()
    workspace = root["workspace_id"]
    if not _identifier(workspace) or not _identifier(snapshot.destination_workspace_id):
        raise _invalid()
    artifacts = by_id["artifacts"]
    for row in artifacts.values():
        if row["state"] == "deleted" or not _identifier(row["workspace_id"]):
            raise _invalid()
        if row["session_id"] in sessions and (
            row["project_id"] != root["id"] or row["workspace_id"] != workspace
        ):
            raise _invalid()
        if row["session_id"] is not None and row["project_id"] is None:
            raise _invalid()
    for table in (
        "documents",
        "generation_plans",
        "session_sources",
        "sources",
        "session_settings",
        "outcome_plans",
        "session_stage_selections",
        "knowledge_ledgers",
        "subtitle_evidence",
        "project_speech_references",
    ):
        if any(row["session_id"] not in sessions for row in rows[table]):
            raise _invalid()
    ledger_scopes = set()
    for row in rows["knowledge_ledgers"]:
        kind = row["kind"]
        source_language = row["source_language"]
        target_language = row["target_language"]
        if (
            kind not in {"research", "glossary", "generation_controls"}
            or type(source_language) is not str
            or not source_language
            or source_language != source_language.strip().lower()
            or len(source_language) > 40
            or type(target_language) is not str
            or target_language != target_language.strip().lower()
            or len(target_language) > 40
        ):
            raise _invalid()
        scope = (row["session_id"], kind, source_language, target_language)
        if scope in ledger_scopes:
            raise _invalid()
        ledger_scopes.add(scope)
        if kind == "generation_controls":
            try:
                freeze_generation_controls(row)
            except (TypeError, ValueError, KeyError) as error:
                raise _invalid() from error
    for row in rows["documents"]:
        active = row["active_revision_id"]
        if active is not None and (
            active not in by_id["document_revisions"]
            or by_id["document_revisions"][active]["document_id"] != row["id"]
        ):
            raise _invalid()
    numbers = set()
    for row in rows["document_revisions"]:
        if row["document_id"] not in by_id["documents"]:
            raise _invalid()
        number = (row["document_id"], row["revision_number"])
        if number in numbers:
            raise _invalid()
        numbers.add(number)
        parent = by_id["document_revisions"].get(row["parent_revision_id"])
        if parent and parent["document_id"] != row["document_id"]:
            raise _invalid()
    for table in ("segments", "timed_words"):
        ordinals = set()
        for row in rows[table]:
            ordinal = (row["revision_id"], row["ordinal"])
            if row["revision_id"] not in by_id["document_revisions"] or ordinal in ordinals:
                raise _invalid()
            ordinals.add(ordinal)
            if table == "timed_words" and row["segment_id"] is not None:
                segment = by_id["segments"].get(row["segment_id"])
                if segment is None or segment["revision_id"] != row["revision_id"]:
                    raise _invalid()
    for table, target, columns in (
        ("segment_lineage", "segments", ("parent_segment_id", "child_segment_id")),
        ("artifact_edges", "artifacts", ("parent_artifact_id", "child_artifact_id")),
    ):
        if any(row[key] not in by_id[target] for row in rows[table] for key in columns):
            raise _invalid()
    for row in rows["source_assets"]:
        artifact = artifacts.get(row["artifact_id"])
        if (
            artifact is None
            or row["workspace_id"] != workspace
            or artifact["workspace_id"] != workspace
            or row["state"] == "deleted"
        ):
            raise _invalid()
    attachments = set()
    for row in rows["session_sources"]:
        key = (row["session_id"], row["source_asset_id"], row["role"])
        if (
            row["source_asset_id"] not in by_id["source_assets"]
            or row["is_current"] is not True
            or key in attachments
        ):
            raise _invalid()
        attachments.add(key)
    if any(row["artifact_id"] not in artifacts for row in rows["sources"]):
        raise _invalid()
    for row in rows["session_stage_selections"]:
        artifact = artifacts.get(row["artifact_id"])
        if row["artifact_id"] is not None and (
            artifact is None or artifact["session_id"] != row["session_id"]
        ):
            raise _invalid()
    sections = {(row["session_id"], row["section"]) for row in rows["session_settings"]}
    if any(
        row["section"] not in {*SETTING_SECTIONS, "multilingual_setup"}
        for row in rows["session_settings"]
    ):
        raise _invalid()
    if not {(sid, section) for sid in sessions for section in SETTING_SECTIONS} <= sections:
        raise _invalid()
    groups = rows["translation_projects"]
    if len(groups) > 1 or (not groups and rows["translation_project_branches"]):
        raise _invalid()
    for group in groups:
        artifact = artifacts.get(group["checkpoint_artifact_id"])
        if (
            group["project_root_id"] != root["id"]
            or group["source_session_id"] != source
            or artifact is None
            or artifact["session_id"] != source
        ):
            raise _invalid()
        if not _same_hash(group["source_content_hash"], artifact["content_hash"]):
            raise _invalid()
    branch_sessions, languages = set(), set()
    for row in rows["translation_project_branches"]:
        artifact = artifacts.get(row["source_checkpoint_artifact_id"])
        if (
            not groups
            or row["project_id"] != groups[0]["id"]
            or row["session_id"] == source
            or row["session_id"] not in sessions
            or row["session_id"] in branch_sessions
            or row["target_language"] in languages
        ):
            raise _invalid()
        if (
            artifact is None
            or artifact["session_id"] != row["session_id"]
            or not _same_hash(row["source_content_hash"], artifact["content_hash"])
        ):
            raise _invalid()
        branch_sessions.add(row["session_id"])
        languages.add(row["target_language"])
    try:
        validate_media_graph(
            rows,
            branch_source_sessions=(
                {
                    branch["session_id"]: groups[0]["source_session_id"]
                    for branch in rows["translation_project_branches"]
                }
                if groups
                else {}
            ),
            document_revision_ids=set(by_id["document_revisions"]),
        )
    except (TypeError, ValueError, KeyError) as error:
        raise _invalid() from error
    try:
        validate_evidence_graph(rows)
    except (TypeError, ValueError) as error:
        raise _invalid() from error
    try:
        validate_speech_copy_graph(rows, allow_null_inputs=allow_null_inputs,
                                   allow_segment_references=allow_segment_references)
        validate_speech_plan_graph(rows, allow_segment_references=allow_segment_references)
        validate_audio_copy_graph(rows, allow_saved_selections=allow_saved_selections)
    except (TypeError, ValueError, KeyError) as error:
        raise _invalid() from error
    return root, source


def _same_hash(left: Any, right: Any) -> bool:
    return (
        type(left) is str
        and type(right) is str
        and _HASH.fullmatch(left) is not None
        and _HASH.fullmatch(right) is not None
        and left.casefold() == right.casefold()
    )


def _relative(value: Any) -> PurePosixPath:
    if type(value) is not str or not value or "\\" in value:
        raise _invalid()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or PureWindowsPath(value).drive
        or path.as_posix() != value
        or any(part in {".", ".."} for part in path.parts)
    ):
        raise _invalid()
    return path


def _staging(snapshot, staged, by_id, copy_id):
    if (
        type(staged) is not StagedProjectFiles
        or type(staged.files) is not tuple
        or type(snapshot.files) is not tuple
    ):
        raise _invalid()
    if staged.snapshot_fingerprint != snapshot.fingerprint:
        raise _invalid()
    if (
        type(staged.directory_device) is not int
        or type(staged.directory_inode) is not int
        or staged.directory_device < 0
        or staged.directory_inode < 0
    ):
        raise _invalid()
    directory = _relative(staged.directory_relative_path)
    if (
        len(directory.parts) != 4
        or directory.parts[:3] != ("artifacts", "project-copies", copy_id)
        or not directory.name.startswith("attempt-")
    ):
        raise _invalid()
    artifacts = by_id["artifacts"]
    sources = {}
    for source in snapshot.files:
        if (
            type(source) is not CopyFileSource
            or not _identifier(source.artifact_id)
            or source.artifact_id in sources
            or source.artifact_id not in artifacts
        ):
            raise _invalid()
        row = artifacts[source.artifact_id]
        if any(
            getattr(source, key) != row[key]
            for key in ("workspace_id", "project_id", "session_id", "relative_path", "size_bytes")
        ) or not _same_hash(source.content_hash, row["content_hash"]):
            raise _invalid()
        sources[source.artifact_id] = source
    if set(sources) != set(artifacts):
        raise _invalid()
    proofs, paths, targets = {}, set(), set()
    for file in staged.files:
        if (
            type(file) is not StagedCopyFile
            or not _identifier(file.source_artifact_id)
            or file.source_artifact_id not in sources
            or file.source_artifact_id in proofs
        ):
            raise _invalid()
        source = sources[file.source_artifact_id]
        target = copy_identity(copy_id, "artifacts", file.source_artifact_id)
        if (
            file.artifact_id != target
            or target in targets
            or not _same_hash(file.content_hash, source.content_hash)
            or type(file.size_bytes) is not int
            or file.size_bytes < 0
        ):
            raise _invalid()
        if source.size_bytes is not None and source.size_bytes != file.size_bytes:
            raise _invalid()
        path = _relative(file.relative_path)
        suffix = PurePosixPath(source.relative_path).suffix
        suffix = suffix if _SUFFIX.fullmatch(suffix) else ".bin"
        if path.parent != directory or path.name != target + suffix or file.relative_path in paths:
            raise _invalid()
        proofs[file.source_artifact_id] = file
        paths.add(file.relative_path)
        targets.add(target)
    if set(proofs) != set(sources):
        raise _invalid()
    return proofs


def _excluded(key: str) -> bool:
    key = key.lower()
    return (
        connection_key(key)
        or key in {"path", "paths", "job_id", "job_ids", "run_id", "run_ids"}
        or key.endswith(("_path", "_paths", "_job_id", "_job_ids", "_run_id", "_run_ids"))
    )


def _remap_json(
    value: Any,
    identities: Mapping[str, str],
    proofs: Mapping[str, StagedCopyFile],
    *,
    key: str = "",
) -> Any:
    reference = key == "id" or key.endswith(("_id", "_ids"))
    managed = _REFERENCE_FAMILIES.search(key) is not None
    if isinstance(value, str):
        if reference and value in identities:
            return identities[value]
        if reference and managed and value and _is_uuid(value):
            raise _invalid()
        return value
    if isinstance(value, list):
        return [_remap_json(item, identities, proofs, key=key) for item in value]
    if isinstance(value, dict):
        result = {}
        for child_key, item in _secret_free(value).items():
            if _excluded(child_key):
                continue
            mapped_key = identities.get(child_key, child_key)
            if reference and managed and _is_uuid(child_key) and child_key not in identities:
                raise _invalid()
            result[mapped_key] = _remap_json(item, identities, proofs, key=child_key)
        # Normalize only equivalent, explicitly paired file digests. Arbitrary
        # human/hash strings and document revision hashes remain untouched.
        for artifact_key, old_id in value.items():
            if (
                not artifact_key.endswith("artifact_id")
                or type(old_id) is not str
                or old_id not in proofs
            ):
                continue
            prefix = artifact_key[: -len("artifact_id")]
            candidates = {prefix + "content_hash", prefix + "sha256"}
            if artifact_key in {"checkpoint_artifact_id", "source_checkpoint_artifact_id"}:
                candidates.add("source_content_hash")
            for hash_key in candidates:
                if hash_key in result and _same_hash(result[hash_key], proofs[old_id].content_hash):
                    result[hash_key] = proofs[old_id].content_hash.casefold()
        return result
    return value


def validate_project_copy_snapshot(snapshot: ProjectCopySnapshot) -> None:
    """Prove the frozen row graph before the worker starts staging any files."""
    rows, by_id, _all_ids = _decode(snapshot)
    version = json.loads(snapshot.state_json)["schema_version"]
    _references(
        rows, by_id, snapshot, allow_null_inputs=version >= 11,
        allow_segment_references=version >= 12, allow_saved_selections=version >= 13,
    )


def _prepare(rows, identities, proofs, snapshot, source_id, created_by, copy_id):
    mapped = {table: [] for table in _FIELDS}
    version = json.loads(snapshot.state_json)["schema_version"]
    audio_metadata = (remap_audio_metadata(rows, identities, allow_saved_selections=version >= 13)
                      if version >= 9 else {})
    new_root = identities[snapshot.source_project_id]
    session_ids = {row["id"] for row in rows["sessions"]}
    revision_ids = {row["id"] for row in rows["document_revisions"]}
    media_revision_ids = {row["id"] for row in rows["media_edit_plan_revisions"]}
    sound_artifact_ids = {row["artifact_id"] for row in rows["sound_variants"]}
    for table, values in rows.items():
        for original in values:
            if (table in SPEECH_FAMILIES or table in SOUND_FAMILIES
                    or table in PERFORMANCE_FAMILIES or table == "audio_takes"):
                continue
            if table == "project_speech_references":
                try:
                    if original["slot_key"] == "tts":
                        remap = remap_tts_reference
                    elif version >= 12 and original["slot_key"].startswith("segment:"):
                        remap = remap_segment_reference
                    else:
                        remap = remap_cast_reference
                    mapped[table].append(remap(original, identities, allow_null_inputs=version >= 11))
                except (TypeError, ValueError, KeyError) as error:
                    raise _invalid() from error
                continue
            if table == "knowledge_ledgers" and original["kind"] == "generation_controls":
                try:
                    payload = freeze_generation_controls(original)
                except (TypeError, ValueError, KeyError) as error:
                    raise _invalid() from error
                mapped[table].append({**original, "id": identities[original["id"]],
                                      "session_id": identities[original["session_id"]],
                                      "payload_json": payload, "revision": 1})
                continue
            if table == "subtitle_evidence":
                mapped[table].append(
                    remap_evidence(
                        original,
                        identities,
                        proofs,
                        copy_id=copy_id,
                    )
                )
                continue
            if table == "media_edit_plan_revisions":
                try:
                    mapped[table].append(remap_revision(original, identities, media_revision_ids))
                except (TypeError, ValueError, KeyError) as error:
                    raise _invalid() from error
                continue
            row = dict(original)
            for field, item in original.items():
                if table == "artifacts" and field == "metadata_json" and original["id"] in audio_metadata:
                    row[field] = audio_metadata[original["id"]]
                elif field.endswith("_json"):
                    row[field] = _remap_json(item, identities, proofs)
                elif field == "id" or field.endswith("_id"):
                    row[field] = identities.get(item, item) if item is not None else None
            if table == "projects":
                row.update(
                    id=new_root,
                    workspace_id=snapshot.destination_workspace_id,
                    name=snapshot.name,
                    created_by_user_id=created_by,
                    source_session_id=None,
                    revision=1,
                )
            elif table == "sessions":
                row.update(
                    project_id=new_root,
                    storage_key=row["id"],
                    status="idle",
                    revision=1,
                    legacy_name=None,
                    legacy_path=None,
                    trashed_at=None,
                    purge_after=None,
                )
                if original["id"] == source_id:
                    row["name"] = snapshot.name
            elif table == "artifacts":
                proof = proofs[original["id"]]
                row.update(
                    workspace_id=snapshot.destination_workspace_id,
                    project_id=new_root,
                    session_id=identities.get(original["session_id"])
                    if original["session_id"] in session_ids
                    else None,
                    relative_path=proof.relative_path,
                    content_hash=proof.content_hash.casefold(),
                    size_bytes=proof.size_bytes,
                )
                if original["id"] in sound_artifact_ids:
                    row.update(project_id=None, session_id=None)
            elif table in {"source_assets", "sources"}:
                row.update(
                    external_path=None,
                    content_hash=proofs[original["artifact_id"]].content_hash.casefold(),
                )
                if table == "source_assets":
                    row["workspace_id"] = snapshot.destination_workspace_id
            elif table == "translation_projects":
                row.update(
                    name=snapshot.name,
                    revision=1,
                    source_content_hash=proofs[
                        original["checkpoint_artifact_id"]
                    ].content_hash.casefold(),
                )
            elif table == "translation_project_branches":
                row["source_content_hash"] = proofs[
                    original["source_checkpoint_artifact_id"]
                ].content_hash.casefold()
            elif table in {"session_settings", "outcome_plans", "session_stage_selections"}:
                row["revision"] = 1
            elif table == "document_revisions":
                row["parent_revision_id"] = (
                    identities.get(original["parent_revision_id"])
                    if original["parent_revision_id"] in revision_ids
                    else None
                )
            elif table == "knowledge_ledgers":
                try:
                    KnowledgeLedgerStore._validated_payload(row["kind"], row["payload_json"])
                except (KnowledgeValidationError, TypeError, ValueError) as error:
                    raise _invalid() from error
                row["revision"] = 1
            mapped[table].append(row)
    try:
        mapped["audio_takes"] = remap_audio_rows(rows, identities)
        mapped.update(remap_speech_plan_rows(rows, identities, allow_segment_references=version >= 12))
        if version >= 14:
            mapped.update(remap_sound_copy_rows(
                rows, identities, destination_workspace_id=snapshot.destination_workspace_id
            ))
        if version >= 15:
            mapped.update(remap_performance_plan_rows(rows, identities, mapped_rows=mapped))
    except (TypeError, ValueError, KeyError) as error:
        raise _invalid() from error
    rebind_published_metadata(rows, mapped, identities)
    branch_source_sessions = (
        {
            branch["session_id"]: mapped["translation_projects"][0]["source_session_id"]
            for branch in mapped["translation_project_branches"]
        }
        if mapped["translation_projects"]
        else {}
    )
    try:
        validate_media_graph(
            mapped,
            branch_source_sessions=branch_source_sessions,
            document_revision_ids={row["id"] for row in mapped["document_revisions"]},
        )
    except (TypeError, ValueError, KeyError) as error:
        raise _invalid() from error
    try:
        validate_speech_copy_graph(mapped, allow_null_inputs=version >= 11,
                                   allow_segment_references=version >= 12)
        validate_speech_plan_graph(mapped, allow_segment_references=version >= 12)
        validate_audio_copy_graph(mapped, allow_saved_selections=version >= 13)
        if version >= 10:
            validate_output_copy_graph(mapped, allow_sound_mix=version >= 14)
        validate_sound_copy_graph(mapped, allow_sounds=version >= 14)
        validate_performance_copy_graph(mapped, allow_performance=version >= 15)
    except (TypeError, ValueError, KeyError) as error:
        raise _invalid() from error
    return mapped


def _insert(session: Session, table: str, rows: list[dict[str, Any]]) -> None:
    for chunk in _chunks(rows):
        session.execute(_TABLES[table].insert(), chunk)


def publish_project_copy_rows(
    session: Session,
    snapshot: ProjectCopySnapshot,
    staged: StagedProjectFiles,
    *,
    copy_id: str,
    created_by_user_id: str,
) -> ProjectCopyResult:
    """Insert a validated graph; the caller owns its immediate writer and result."""
    if not _uuid(copy_id):
        raise _invalid()
    with session.no_autoflush:
        rows, by_id, all_ids = _decode(snapshot)
        state = json.loads(snapshot.state_json)
        _root, source_id = _references(
            rows, by_id, snapshot, allow_null_inputs=state["schema_version"] >= 11,
            allow_segment_references=state["schema_version"] >= 12,
            allow_saved_selections=state["schema_version"] >= 13,
        )
        bindings = state.get("destination_connections", [])
        validate_current_connections(session, snapshot.destination_workspace_id, bindings)
        if (
            type(snapshot.name) is not str
            or not snapshot.name
            or snapshot.name != snapshot.name.strip()
            or len(snapshot.name) > 255
        ):
            raise _invalid()
        if (
            not _identifier(created_by_user_id)
            or session.scalar(select(m.User.id).where(m.User.id == created_by_user_id)) is None
        ):
            raise _invalid()
        if (
            session.scalar(
                select(m.Workspace.id).where(m.Workspace.id == snapshot.destination_workspace_id)
            )
            is None
        ):
            raise _invalid()
        identities = {
            old_id: copy_identity(copy_id, table, old_id)
            for table in _ID_TABLES
            for old_id in by_id[table]
        }
        if len(set(identities.values())) != len(identities) or set(identities.values()) & all_ids:
            raise _invalid()
        proofs = _staging(snapshot, staged, by_id, copy_id)
        prepared = _prepare(
            rows, identities, proofs, snapshot, source_id, created_by_user_id, copy_id
        )
        mapped_bindings = [
            {
                **binding,
                "targets": [
                    {**target, "session_id": identities[target["session_id"]]}
                    for target in binding["targets"]
                ],
            }
            for binding in bindings
        ]
        mapped_state = {**state, "rows": prepared}
        if state["schema_version"] in {6, 7, 8, 9, 10, 11, 12, 13, 14, 15}:
            mapped_state["destination_connections"] = mapped_bindings
        validate_connection_state(mapped_state, prepared)
        # Collision checks, JSON-reference validation and every proof check finish
        # before the first INSERT. No partially prepared root can escape them.
        for table in _ID_TABLES:
            model = _MODELS[table]
            for chunk in _chunks(identities[old_id] for old_id in by_id[table]):
                if session.scalar(select(model.id).where(model.id.in_(chunk)).limit(1)) is not None:
                    raise _invalid()
        for chunk in _chunks(identities[sid] for sid in by_id["sessions"]):
            if (
                session.scalar(
                    select(m.SessionRecord.id)
                    .where(m.SessionRecord.storage_key.in_(chunk))
                    .limit(1)
                )
                is not None
            ):
                raise _invalid()
        for chunk in _chunks(file.relative_path for file in proofs.values()):
            if (
                session.scalar(
                    select(m.Artifact.id).where(m.Artifact.relative_path.in_(chunk)).limit(1)
                )
                is not None
            ):
                raise _invalid()
        _insert(session, "projects", prepared["projects"])
        source_rows = [row for row in prepared["sessions"] if row["id"] == identities[source_id]]
        children = [row for row in prepared["sessions"] if row["id"] != identities[source_id]]
        _insert(session, "sessions", source_rows)
        _insert(session, "sessions", children)
        session.execute(
            update(_TABLES["projects"])
            .where(m.Project.id == identities[snapshot.source_project_id])
            .values(source_session_id=identities[source_id])
        )
        _insert(session, "documents", prepared["documents"])
        parents = []
        revisions = []
        for row in prepared["document_revisions"]:
            revisions.append({**row, "parent_revision_id": None})
            if row["parent_revision_id"] is not None:
                parents.append(
                    {"_copy_revision_id": row["id"], "_copy_parent_id": row["parent_revision_id"]}
                )
        _insert(session, "document_revisions", revisions)
        if parents:
            statement = (
                update(_TABLES["document_revisions"])
                .where(m.DocumentRevision.id == bindparam("_copy_revision_id"))
                .values(parent_revision_id=bindparam("_copy_parent_id"))
            )
            for chunk in _chunks(parents):
                session.execute(statement, chunk)
        for table in (
            "segments",
            "timed_words",
            "segment_lineage",
            "artifacts",
        ):
            _insert(session, table, prepared[table])
        plans = [{**row, "active_revision_id": None} for row in prepared["media_edit_plans"]]
        _insert(session, "media_edit_plans", plans)
        media_parents = []
        media_revisions = []
        for row in prepared["media_edit_plan_revisions"]:
            media_revisions.append({**row, "parent_revision_id": None})
            if row["parent_revision_id"] is not None:
                media_parents.append(
                    {"_copy_revision_id": row["id"], "_copy_parent_id": row["parent_revision_id"]}
                )
        _insert(session, "media_edit_plan_revisions", media_revisions)
        if media_parents:
            statement = (
                update(_TABLES["media_edit_plan_revisions"])
                .where(m.MediaEditPlanRevision.id == bindparam("_copy_revision_id"))
                .values(parent_revision_id=bindparam("_copy_parent_id"))
            )
            for chunk in _chunks(media_parents):
                session.execute(statement, chunk)
        active_plans = [
            {
                "_copy_plan_id": row["id"],
                "_copy_active_id": row["active_revision_id"],
            }
            for row in prepared["media_edit_plans"]
            if row["active_revision_id"] is not None
        ]
        if active_plans:
            statement = (
                update(_TABLES["media_edit_plans"])
                .where(m.MediaEditPlan.id == bindparam("_copy_plan_id"))
                .values(active_revision_id=bindparam("_copy_active_id"))
            )
            for chunk in _chunks(active_plans):
                session.execute(statement, chunk)
        _insert(session, "generation_plans", [
            {**row, "active_revision_id": None} for row in prepared["generation_plans"]
        ])
        _insert(session, "generation_plan_revisions", prepared["generation_plan_revisions"])
        _insert(session, "generation_segments", prepared["generation_segments"])
        speech_active = [{"_copy_plan_id": row["id"], "_copy_active_id": row["active_revision_id"]}
                         for row in prepared["generation_plans"]]
        if speech_active:
            statement = (update(_TABLES["generation_plans"])
                         .where(m.GenerationPlan.id == bindparam("_copy_plan_id"))
                         .values(active_revision_id=bindparam("_copy_active_id")))
            for chunk in _chunks(speech_active):
                session.execute(statement, chunk)
        _insert(session, "speech_plan_reviews", prepared["speech_plan_reviews"])
        _insert(session, "audio_takes", prepared["audio_takes"])
        _insert(session, "performance_plans", prepared["performance_plans"])
        for table in ("sound_variants", "project_sound_plans", "project_sound_cues"):
            _insert(session, table, prepared[table])
        for table in (
            "artifact_edges",
            "source_assets",
            "session_sources",
            "sources",
            "translation_projects",
            "translation_project_branches",
            "session_settings",
            "outcome_plans",
            "session_stage_selections",
            "knowledge_ledgers",
            "subtitle_evidence",
            "project_speech_references",
        ):
            _insert(session, table, prepared[table])
        group = prepared["translation_projects"]
        return ProjectCopyResult(
            identities[snapshot.source_project_id],
            identities[source_id],
            group[0]["id"] if group else None,
            snapshot.destination_workspace_id,
            snapshot.name,
            snapshot.fingerprint,
        )
