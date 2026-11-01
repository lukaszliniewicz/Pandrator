"""Capture, prepare, publish and load independently owned speech inputs.

Callers own membership/grants, a fresh publication writer, and staged-file
lifetime after commit or rollback. Preparation and loading never acquire a
provider, discover credentials, or consult a mutable library after publication.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .database import Database
from .models import Artifact, ArtifactEdge, Voice, VoiceSample
from .projects.artifact_identity import resolve_artifact_ownership
from .speech_input_contracts import (
    AUDIO_LIMIT,
    JSON_LIMIT,
    SHA256,
    TEXT_LIMIT,
    FileIdentity,
    PreparedSpeechInput,
    SpeechInputCapture,
    SpeechInputError,
    SpeechInputFile,
    SpeechInputOwner,
    canonical_json,
    validate_speech_input_pin,
)
from .speech_input_files import (
    cleanup_files,
    create_staging_directory,
    file_identity,
    read_verified_file,
    stage_file,
    validate_wav,
)
from .voice_identity import voice_sample_workspace, voice_workspace

_ENVELOPE_FIELDS = frozenset(
    {
        "version",
        "adapter",
        "model",
        "voice",
        "display_name",
        "reference_text",
        "reference_language",
        "audio_sha256",
        "source_fingerprint",
    }
)
_ARTIFACT_FIELDS = (
    "id",
    "workspace_id",
    "project_id",
    "session_id",
    "kind",
    "role",
    "state",
    "relative_path",
    "content_hash",
    "size_bytes",
    "mime_type",
)


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def speech_input_adapter(settings: dict[str, Any]) -> tuple[str, set[str]]:
    """Resolve the selected adapter and its linked-reference registration aliases."""
    # This only resolves the frozen settings catalogue. Never resolve a secret.
    from pandrator.logic import tts_handler

    explicit = _normalized(settings.get("preview_service_id") or settings.get("service_id"))
    service = _normalized(settings.get("service") or settings.get("tts_service"))
    selected = explicit or service
    registration_ids = {"audio_cpp"}
    if selected in {"audio_cpp", "audiocpp"}:
        return "audio_cpp", registration_ids
    if _normalized(settings.get("preview_adapter")) == "audio_cpp":
        if explicit:
            registration_ids.add(explicit)
        return "audio_cpp", registration_ids
    if selected in {"custom", "openai_compatible"} or service in {"custom", "openai_compatible"}:
        endpoint, _error = tts_handler.resolve_openai_audio_endpoint(settings, {})
        if endpoint is not None:
            if _normalized(endpoint.get("adapter")) == "audio_cpp":
                registration_ids.add(_normalized(endpoint.get("name")))
                return "audio_cpp", registration_ids
    return selected, registration_ids


def _owner(session: Session, owner: SpeechInputOwner) -> None:
    try:
        resolved = resolve_artifact_ownership(
            session,
            workspace_id=owner.workspace_id,
            project_id=owner.project_id,
            session_id=owner.session_id,
        )
        if resolved.workspace_id != owner.workspace_id or resolved.project_id != owner.project_id:
            raise SpeechInputError("Speech input owner roots disagree.")
    except (ValueError, KeyError) as error:
        raise SpeechInputError("Speech input owner roots disagree.") from error


def _row(artifact: Artifact) -> str:
    return canonical_json({key: getattr(artifact, key) for key in _ARTIFACT_FIELDS})


def _rooted(session: Session, artifact: Artifact, owner: SpeechInputOwner) -> None:
    if (artifact.workspace_id, artifact.project_id, artifact.session_id) != (
        owner.workspace_id,
        owner.project_id,
        owner.session_id,
    ) or artifact.state != "current":
        raise SpeechInputError("Speech input artifact has a different owner or state.")
    try:
        resolve_artifact_ownership(session, artifact=artifact)
    except (ValueError, KeyError) as error:
        raise SpeechInputError("Speech input artifact roots disagree.") from error


def _scoped_artifact(session: Session, identifier: str, owner: SpeechInputOwner) -> Artifact:
    artifact = session.scalar(
        select(Artifact).where(
            Artifact.id == identifier,
            Artifact.workspace_id == owner.workspace_id,
            Artifact.project_id == owner.project_id,
            Artifact.session_id == owner.session_id,
            Artifact.state == "current",
        )
    )
    if artifact is None:
        raise SpeechInputError("Speech input artifact is unavailable in this owner.")
    _rooted(session, artifact, owner)
    return artifact


def _source(
    session: Session,
    paths: DataPaths,
    settings: dict[str, Any],
    owner: SpeechInputOwner,
    voice_id: str | None,
) -> tuple[str, str, str] | None:
    adapter, registration_ids = speech_input_adapter(settings)
    explicit_voice = None
    if voice_id is not None:
        if type(voice_id) is not str or not voice_id.strip() or len(voice_id.strip()) > 80:
            raise SpeechInputError("Invalid managed voice selection.")
        voice_id = voice_id.strip()
        explicit_voice = session.scalar(
            select(Voice).where(
                Voice.id == voice_id,
                Voice.workspace_id == owner.workspace_id,
            )
        )
        if explicit_voice is None:
            raise SpeechInputError("Managed voice is unavailable in this workspace.")
        voice_workspace(session, explicit_voice, workspace_id=owner.workspace_id)
    if adapter != "audio_cpp" or (voice_id is None and "audio_cpp_voice_ref" in settings):
        return None
    selected = str(settings.get("voice") or settings.get("speaker") or "").strip()
    if not selected:
        if voice_id is not None:
            raise SpeechInputError("Managed speech input has no selected voice alias.")
        return None
    voices = (
        [explicit_voice]
        if explicit_voice is not None
        else session.scalars(
            select(Voice).where(Voice.workspace_id == owner.workspace_id).order_by(Voice.id)
        ).all()
    )
    matches: list[tuple[Voice, dict[str, Any]]] = []
    for voice in voices:
        for provider, registration in dict(
            (voice.metadata_json or {}).get("providers") or {}
        ).items():
            if _normalized(provider) not in registration_ids or type(registration) is not dict:
                continue
            if (
                registration.get("resource_kind") != "linked_reference"
                or registration.get("status") != "ready"
            ):
                continue
            names = {
                voice.name,
                str(registration.get("voice_id") or ""),
                str(registration.get("provider_voice_id") or ""),
            }
            if selected.casefold() in {name.strip().casefold() for name in names if name.strip()}:
                matches.append((voice, {"provider": provider, "registration": registration}))
                break
    if len(matches) > 1:
        raise SpeechInputError("The selected audio.cpp voice alias is ambiguous.")
    if not matches:
        if voice_id is not None:
            raise SpeechInputError("Managed voice does not match the selected ready reference.")
        return None
    voice, registration = matches[0]
    voice_workspace(session, voice, workspace_id=owner.workspace_id)
    for sample in session.scalars(
        select(VoiceSample)
        .where(
            VoiceSample.voice_id == voice.id,
        )
        .order_by(VoiceSample.created_at.desc(), VoiceSample.id.desc())
    ).all():
        # Validate current persisted roots before reading transcript/artifact body or filesystem.
        try:
            voice_sample_workspace(session, sample, workspace_id=owner.workspace_id)
        except (ValueError, KeyError) as error:
            raise SpeechInputError("Selected voice sample ownership disagrees.") from error
        artifact = session.get(Artifact, sample.artifact_id)
        if artifact is None or artifact.state != "current":
            continue
        try:
            identity = file_identity(paths, artifact.relative_path)
        except SpeechInputError as error:
            # A missing file may be an older unavailable reference. Unsafe/nonregular
            # material is always rejected, rather than silently choosing another sample.
            if isinstance(error.__cause__, FileNotFoundError):
                continue
            raise
        if not artifact.relative_path.lower().endswith(".wav") or identity.size > AUDIO_LIMIT:
            raise SpeechInputError("Speech reference must be WAV audio of at most 5 MiB.")
        reference_text = str(sample.transcript or "").strip() if sample.transcript_reviewed else ""
        if len(reference_text.encode("utf-8")) > TEXT_LIMIT:
            raise SpeechInputError("Reviewed speech reference text is too large.")
        state = {
            "voice": {
                "id": voice.id,
                "workspace_id": voice.workspace_id,
                "revision": voice.revision,
                "name": voice.name,
                "language": voice.language,
            },
            "selected_registration": registration,
            "sample": {
                "id": sample.id,
                "voice_id": sample.voice_id,
                "workspace_id": sample.workspace_id,
                "artifact_id": sample.artifact_id,
                "transcript": sample.transcript,
                "transcript_language": sample.transcript_language,
                "transcript_reviewed": sample.transcript_reviewed,
                "created_at": sample.created_at.isoformat(),
            },
            "artifact": json.loads(_row(artifact)),
        }
        envelope = {
            "version": 1,
            "adapter": "audio_cpp",
            "model": str(settings.get("model") or ""),
            "voice": selected,
            "display_name": voice.name,
            "reference_text": reference_text,
            "reference_language": str(sample.transcript_language or voice.language or "")
            if sample.transcript_reviewed
            else "",
        }
        return canonical_json(state), canonical_json(envelope), artifact.relative_path
    raise SpeechInputError("Selected linked voice has no readable sample.")


def capture_speech_input(
    session: Session,
    paths: DataPaths,
    settings: dict[str, Any],
    *,
    owner: SpeechInputOwner,
    voice_id: str | None = None,
) -> SpeechInputCapture:
    _owner(session, owner)
    copied = deepcopy(settings)
    try:
        source = _source(session, paths, copied, owner, voice_id)
    except (ValueError, KeyError, OSError) as error:
        raise SpeechInputError(str(error)) from error
    if source is None:
        return SpeechInputCapture(owner, canonical_json(copied), False)
    state, envelope, relative = source
    for key in ("audio_cpp_voice_ref", "audio_cpp_voice_ref_hash", "audio_cpp_reference_text"):
        copied.pop(key, None)
    settings_json = canonical_json(copied)
    return SpeechInputCapture(
        owner,
        settings_json,
        True,
        _digest(state),
        state,
        relative,
        file_identity(paths, relative),
        envelope,
        voice_id,
    )


def _envelope(data: bytes) -> dict[str, Any]:
    if len(data) > JSON_LIMIT:
        raise SpeechInputError("Speech input envelope is too large.")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise SpeechInputError("Duplicate speech input envelope field.")
            result[key] = value
        return result

    try:
        value = json.loads(data, object_pairs_hook=pairs)
        if type(value) is not dict or set(value) != _ENVELOPE_FIELDS:
            raise SpeechInputError("Invalid speech input envelope fields.")
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["adapter"] != "audio_cpp"
        ):
            raise SpeechInputError("Invalid speech input envelope version or adapter.")
        for key in _ENVELOPE_FIELDS - {"version"}:
            if type(value[key]) is not str:
                raise SpeechInputError("Invalid speech input envelope value.")
        if len(value["reference_text"].encode("utf-8")) > TEXT_LIMIT:
            raise SpeechInputError("Reviewed speech reference text is too large.")
        for key in ("audio_sha256", "source_fingerprint"):
            if SHA256.fullmatch(value[key]) is None:
                raise SpeechInputError("Invalid speech input envelope digest.")
        material = {key: item for key, item in value.items() if key != "source_fingerprint"}
        if _digest(canonical_json(material)) != value["source_fingerprint"]:
            raise SpeechInputError("Speech input envelope fingerprint disagrees.")
        return value
    except (UnicodeError, ValueError, TypeError) as error:
        raise SpeechInputError("Invalid speech input envelope.") from error


def _graph(
    session: Session, snapshot_id: str, owner: SpeechInputOwner
) -> tuple[Artifact, Artifact]:
    _owner(session, owner)
    header = _scoped_artifact(session, snapshot_id, owner)
    if (header.kind, header.role, header.mime_type) != (
        "speech_voice_snapshot",
        "speech_input",
        "application/json",
    ):
        raise SpeechInputError("Invalid speech input snapshot role.")
    edges = session.scalars(
        select(ArtifactEdge).where(ArtifactEdge.child_artifact_id == header.id)
    ).all()
    if len(edges) != 1 or edges[0].relation != "speech_reference":
        raise SpeechInputError("Invalid speech input reference edge.")
    audio = _scoped_artifact(session, edges[0].parent_artifact_id, owner)
    if (audio.kind, audio.role, audio.mime_type) != (
        "audio",
        "speech_reference_audio",
        "audio/wav",
    ):
        raise SpeechInputError("Invalid speech input audio role.")
    return header, audio


def _borrowed(
    artifact: Artifact, identity: FileIdentity, digest: str, size: int
) -> SpeechInputFile:
    if artifact.content_hash != digest or artifact.size_bytes != size:
        raise SpeechInputError("Speech input artifact metadata disagrees with bytes.")
    return SpeechInputFile(
        artifact.id, artifact.relative_path, digest, size, identity, False, _row(artifact)
    )


def prepare_speech_input(
    database: Database,
    paths: DataPaths,
    capture: SpeechInputCapture,
    *,
    before_io: Callable[[], None],
) -> PreparedSpeechInput:
    if (
        not capture.requires_files
        or capture.source_state_json is None
        or capture.source_identity is None
        or capture.source_relative_path is None
        or capture.envelope_json is None
        or capture.source_fingerprint is None
    ):
        raise SpeechInputError("Speech input does not require file preparation.")
    owned: list[SpeechInputFile] = []
    directory = None
    directory_identity = None
    try:
        before_io()
        state = json.loads(capture.source_state_json)
        audio_bytes, audio_hash, source_identity = read_verified_file(
            paths,
            capture.source_relative_path,
            limit=AUDIO_LIMIT,
            expected_hash=state["artifact"]["content_hash"] or None,
            expected_identity=capture.source_identity,
        )
        validate_wav(audio_bytes)
        value = json.loads(capture.envelope_json)
        value["audio_sha256"] = audio_hash
        value["source_fingerprint"] = _digest(canonical_json(value))
        envelope_bytes = canonical_json(value).encode("utf-8")
        _envelope(envelope_bytes)
        envelope_hash = _digest(envelope_bytes)
        audio_file = None
        snapshot_file = None
        # Indexed hashes bound candidates to the same content and exact owner.
        with database.snapshot_session() as session:
            _owner(session, capture.owner)
            snapshots = session.scalars(
                select(Artifact).where(
                    Artifact.content_hash == envelope_hash,
                    Artifact.workspace_id == capture.owner.workspace_id,
                    Artifact.project_id == capture.owner.project_id,
                    Artifact.session_id == capture.owner.session_id,
                    Artifact.kind == "speech_voice_snapshot",
                    Artifact.role == "speech_input",
                    Artifact.state == "current",
                )
            ).all()
            graphs: list[tuple[Artifact, Artifact]] = []
            for candidate in snapshots:
                try:
                    graphs.append(_graph(session, candidate.id, capture.owner))
                except SpeechInputError:
                    continue
            audio_candidates = session.scalars(
                select(Artifact).where(
                    Artifact.content_hash == audio_hash,
                    Artifact.workspace_id == capture.owner.workspace_id,
                    Artifact.project_id == capture.owner.project_id,
                    Artifact.session_id == capture.owner.session_id,
                    Artifact.kind == "audio",
                    Artifact.role == "speech_reference_audio",
                    Artifact.state == "current",
                )
            ).all()
            valid_audio_candidates = []
            for item in audio_candidates:
                try:
                    _rooted(session, item, capture.owner)
                    valid_audio_candidates.append(item)
                except SpeechInputError:
                    continue
            audio_candidates = valid_audio_candidates
        for header, audio in graphs:
            before_io()
            try:
                data, digest, identity = read_verified_file(
                    paths, header.relative_path, limit=JSON_LIMIT, expected_hash=envelope_hash
                )
                _envelope(data)
                if data != envelope_bytes:
                    continue
                candidate_snapshot = _borrowed(header, identity, digest, len(data))
            except SpeechInputError:
                continue
            before_io()
            try:
                data, digest, identity = read_verified_file(
                    paths, audio.relative_path, limit=AUDIO_LIMIT, expected_hash=audio_hash
                )
                validate_wav(data)
                audio_file = _borrowed(audio, identity, digest, len(data))
                snapshot_file = candidate_snapshot
                break
            except SpeechInputError:
                continue
        if audio_file is None:
            for audio in audio_candidates:
                before_io()
                try:
                    if audio.mime_type != "audio/wav":
                        continue
                    data, digest, identity = read_verified_file(
                        paths, audio.relative_path, limit=AUDIO_LIMIT, expected_hash=audio_hash
                    )
                    validate_wav(data)
                    audio_file = _borrowed(audio, identity, digest, len(data))
                    break
                except SpeechInputError:
                    continue
        if snapshot_file is None:
            directory, directory_identity = create_staging_directory(paths)
            if audio_file is None:
                audio_file = stage_file(paths, directory, audio_bytes, ".wav")
                owned.append(audio_file)
            snapshot_file = stage_file(paths, directory, envelope_bytes, ".json")
            owned.append(snapshot_file)
        assert audio_file is not None
        before_io()
        return PreparedSpeechInput(
            capture.owner,
            capture.source_state_json,
            source_identity,
            capture.source_fingerprint,
            audio_file,
            snapshot_file,
            directory,
            directory_identity,
            str(paths.root),
        )
    except BaseException:
        cleanup_files(paths, tuple(owned), directory, directory_identity)
        raise


def _publication_file(
    session: Session, paths: DataPaths, item: SpeechInputFile, owner: SpeechInputOwner
) -> None:
    if file_identity(paths, item.relative_path) != item.identity:
        raise SpeechInputError("Prepared speech input file changed.")
    if not item.owned:
        artifact = _scoped_artifact(session, item.artifact_id, owner)
        if _row(artifact) != item.row_json:
            raise SpeechInputError("Prepared speech input artifact changed.")
    elif (
        session.get(Artifact, item.artifact_id) is not None
        or session.scalar(select(Artifact.id).where(Artifact.relative_path == item.relative_path))
        is not None
    ):
        raise SpeechInputError("Prepared speech input is already registered.")


def publish_speech_input(
    session: Session,
    paths: DataPaths,
    capture: SpeechInputCapture,
    prepared: PreparedSpeechInput,
) -> dict[str, Any]:
    _owner(session, capture.owner)
    if (
        prepared.owner != capture.owner
        or prepared.managed_root != str(paths.root)
        or prepared.source_state_json != capture.source_state_json
        or prepared.source_fingerprint != capture.source_fingerprint
    ):
        raise SpeechInputError("Prepared speech input does not match capture.")
    current = capture_speech_input(
        session, paths, capture.settings, owner=capture.owner, voice_id=capture.voice_id
    )
    if current != capture or current.source_identity != prepared.source_identity:
        raise SpeechInputError("Selected speech input changed before publication.")
    _publication_file(session, paths, prepared.audio, capture.owner)
    _publication_file(session, paths, prepared.snapshot, capture.owner)
    if not prepared.snapshot.owned:
        if prepared.audio.owned:
            raise SpeechInputError("Invalid mixed prepared speech input graph.")
        header, audio = _graph(session, prepared.snapshot.artifact_id, capture.owner)
        if header.id != prepared.snapshot.artifact_id or audio.id != prepared.audio.artifact_id:
            raise SpeechInputError("Prepared speech input edge changed.")
    for item, kind, role, mime in (
        (prepared.audio, "audio", "speech_reference_audio", "audio/wav"),
        (prepared.snapshot, "speech_voice_snapshot", "speech_input", "application/json"),
    ):
        if item.owned:
            session.add(
                Artifact(
                    id=item.artifact_id,
                    workspace_id=capture.owner.workspace_id,
                    project_id=capture.owner.project_id,
                    session_id=capture.owner.session_id,
                    kind=kind,
                    role=role,
                    mime_type=mime,
                    relative_path=item.relative_path,
                    content_hash=item.content_hash,
                    size_bytes=item.size_bytes,
                    state="current",
                    metadata_json={},
                )
            )
    if prepared.snapshot.owned:
        session.flush()
        session.add(
            ArtifactEdge(
                parent_artifact_id=prepared.audio.artifact_id,
                child_artifact_id=prepared.snapshot.artifact_id,
                relation="speech_reference",
            )
        )
    pin = validate_speech_input_pin(
        {
            "version": 1,
            "artifact_id": prepared.snapshot.artifact_id,
            "content_hash": prepared.snapshot.content_hash,
        }
    )
    assert pin is not None
    return pin


def check_speech_input(
    session: Session,
    pin: object,
    *,
    owner: SpeechInputOwner,
    audio_sha256: str | None = None,
) -> None:
    """Check the retained graph in a caller-owned current-authority transaction.

    This is a DB-only persistence proof, never a grant or a file-content check.
    """
    validated = validate_speech_input_pin(pin)
    if validated is None:
        return
    with session.no_autoflush:
        header, audio = _graph(session, validated["artifact_id"], owner)
        if header.content_hash != validated["content_hash"]:
            raise SpeechInputError("Speech input pin hash disagrees.")
        if audio_sha256 is not None and (
            type(audio_sha256) is not str
            or SHA256.fullmatch(audio_sha256) is None
            or audio.content_hash != audio_sha256
        ):
            raise SpeechInputError("Speech input audio hash changed.")


def cleanup_speech_input(prepared: PreparedSpeechInput) -> None:
    """Remove staging known never adopted by a durable publication."""
    cleanup_files(
        DataPaths.from_value(prepared.managed_root),
        (prepared.audio, prepared.snapshot),
        prepared.directory_relative_path,
        prepared.directory_identity,
    )


def cleanup_unpublished_speech_input(database: Database, prepared: PreparedSpeechInput) -> None:
    """Clean a failed attempt only after proving its owned bytes are unregistered.

    Call after the publication writer has finished. A commit may have succeeded
    even when its context raised; an unavailable check must preserve the files.
    Borrowed files never belong to this attempt and need no database check.
    """
    owned = tuple(item for item in (prepared.audio, prepared.snapshot) if item.owned)
    if not owned:
        return
    try:
        with database.snapshot_session() as session:
            registered = session.scalar(
                select(Artifact.id).where(
                    or_(
                        Artifact.id.in_([item.artifact_id for item in owned]),
                        Artifact.relative_path.in_([item.relative_path for item in owned]),
                    )
                ).limit(1)
            )
    except Exception:
        return
    if registered is None:
        cleanup_speech_input(prepared)


def load_speech_input(
    database: Database,
    paths: DataPaths,
    pin: object,
    settings: dict[str, Any],
    *,
    owner: SpeechInputOwner,
    before_io: Callable[[], None],
) -> dict[str, Any]:
    validated = validate_speech_input_pin(pin)
    if validated is None:
        return deepcopy(settings)
    with database.snapshot_session() as session:
        header, audio = _graph(session, validated["artifact_id"], owner)
    if header.content_hash != validated["content_hash"]:
        raise SpeechInputError("Speech input pin hash disagrees.")
    before_io()
    data, digest, identity = read_verified_file(
        paths, header.relative_path, limit=JSON_LIMIT, expected_hash=validated["content_hash"]
    )
    _borrowed(header, identity, digest, len(data))
    envelope = _envelope(data)
    adapter, _registrations = speech_input_adapter(settings)
    if (
        adapter,
        str(settings.get("model") or ""),
        str(settings.get("voice") or settings.get("speaker") or "").strip(),
    ) != (envelope["adapter"], envelope["model"], envelope["voice"]):
        raise SpeechInputError("Speech input does not match selected adapter, model or voice.")
    before_io()
    data, digest, identity = read_verified_file(
        paths, audio.relative_path, limit=AUDIO_LIMIT, expected_hash=envelope["audio_sha256"]
    )
    _borrowed(audio, identity, digest, len(data))
    validate_wav(data)
    # Detect a root/row/edge change during material reads before handing bytes on.
    with database.snapshot_session() as session:
        current_header, current_audio = _graph(session, validated["artifact_id"], owner)
        if _row(current_header) != _row(header) or _row(current_audio) != _row(audio):
            raise SpeechInputError("Speech input graph changed while reading.")
    before_io()
    result = deepcopy(settings)
    result["audio_cpp_voice_ref"] = {
        "type": "base64",
        "data": "data:audio/wav;base64," + base64.b64encode(data).decode("ascii"),
    }
    result["audio_cpp_voice_ref_hash"] = digest
    result["audio_cpp_reference_text"] = envelope["reference_text"]
    return result
