"""Bounded, immutable browser-compatible derivatives of managed video sources."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import threading
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService
from .database import Database
from .jobs import JobQueue
from .media_process import (
    MediaProcessCancelled,
    MediaProcessError,
    resolve_ffmpeg_executable,
    resolve_ffprobe_executable,
    run_media_process,
)
from .models import Artifact, ArtifactEdge, Job, SessionRecord, new_id

PREVIEW_VERSION = "v1"
PREVIEW_ROLE = "source_video_preview"
MAX_DURATION_SECONDS = 4 * 60 * 60
MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
PROCESS_TIMEOUT_SECONDS = 30 * 60
PROBE_TIMEOUT_SECONDS = 30


class VideoPreviewUnsupported(ValueError):
    """The source cannot produce a bounded compatible video preview."""


@dataclass(frozen=True)
class VideoInfo:
    duration_seconds: float
    width: int
    height: int
    frame_rate: float
    has_audio: bool
    video_codec: str
    pixel_format: str
    audio_codec: str | None


def _finite_positive(value: Any) -> float:
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _frame_rate(stream: dict[str, Any]) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            number = float(Fraction(str(stream.get(key) or "0")))
        except (OverflowError, ValueError, ZeroDivisionError):
            continue
        if math.isfinite(number) and number > 0:
            return number
    return 0.0


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise MediaProcessCancelled("Video preview processing was canceled.")


def _managed_file(paths: DataPaths, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        raise VideoPreviewUnsupported("The video source is outside managed storage.")
    path = paths.root
    for part in candidate.parts:
        path /= part
        if path.is_symlink():
            raise VideoPreviewUnsupported("The video source contains an unsafe managed path.")
    if not path.exists():
        raise FileNotFoundError(relative)
    if not stat.S_ISREG(path.lstat().st_mode):
        raise VideoPreviewUnsupported("The video source is not a regular managed file.")
    return path


def _source_hash(path: Path, cancel_event: threading.Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            _check_cancel(cancel_event)
            digest.update(chunk)
    _check_cancel(cancel_event)
    return digest.hexdigest()


def probe_video(path: Path, cancel_event: threading.Event | None = None) -> VideoInfo:
    try:
        result = run_media_process(
            [resolve_ffprobe_executable(), "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,pix_fmt,width,height,duration,avg_frame_rate,r_frame_rate:"
             "stream_disposition=attached_pic:format=duration", "-of", "json", str(path)],
            capture_stdout=True, cancel_event=cancel_event, timeout_seconds=PROBE_TIMEOUT_SECONDS,
        )
        payload = json.loads(result.stdout)
        streams = payload.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
        if video is None or (video.get("disposition") or {}).get("attached_pic"):
            raise VideoPreviewUnsupported("No usable first video stream is available; audio playback may be used instead.")
        duration = _finite_positive((payload.get("format") or {}).get("duration"))
        duration = duration or _finite_positive(video.get("duration"))
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        codec = str(video.get("codec_name") or "")
        if width <= 0 or height <= 0 or codec in {"", "unknown", "none"}:
            raise VideoPreviewUnsupported("The first video stream has no usable dimensions or codec.")
        if duration <= 0 or duration > MAX_DURATION_SECONDS:
            raise VideoPreviewUnsupported("Video preview requires a known finite duration of at most four hours; audio playback may be used instead.")
        return VideoInfo(duration, width, height, _frame_rate(video), audio is not None,
                         codec, str(video.get("pix_fmt") or ""),
                         str(audio.get("codec_name") or "") if audio is not None else None)
    except (MediaProcessError, AttributeError, OverflowError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, VideoPreviewUnsupported):
            raise
        raise VideoPreviewUnsupported("The source could not be probed as usable video; audio playback may be used instead.") from error


class VideoPreviewService:
    def __init__(self, database: Database, paths: DataPaths, artifacts: ArtifactService, jobs: JobQueue):
        self.database = database
        self.paths = paths
        self.artifacts = artifacts
        self.jobs = jobs

    def _source(self, artifact_id: str) -> tuple[Artifact, Path]:
        with self.database.session() as session:
            source = session.get(Artifact, artifact_id)
            if source is None or source.state == "deleted":
                raise KeyError(artifact_id)
            path = _managed_file(self.paths, source.relative_path)
            session.expunge(source)
            return source, path

    @staticmethod
    def _writable_source(session: Session, initial: Artifact, source_hash: str) -> Artifact:
        current = session.get(Artifact, initial.id)
        if current is None or current.state == "deleted":
            raise KeyError(initial.id)
        if (current.relative_path != initial.relative_path or current.session_id != initial.session_id
                or (current.content_hash is not None and current.content_hash != source_hash)):
            raise ValueError("The video source changed; request its preview again.")
        if current.session_id:
            owner = session.get(SessionRecord, current.session_id)
            if owner is None or owner.trashed_at is not None or owner.status == "purging":
                raise ValueError("Restore the session before preparing its video preview.")
        return current

    def _cached(self, session: Session, source: Artifact, source_hash: str) -> Artifact | None:
        for preview in session.scalars(select(Artifact).join(
            ArtifactEdge, ArtifactEdge.child_artifact_id == Artifact.id).where(
                ArtifactEdge.parent_artifact_id == source.id, Artifact.role == PREVIEW_ROLE,
                Artifact.state == "current").order_by(Artifact.created_at.desc(), Artifact.id.desc())).all():
            metadata = preview.metadata_json or {}
            if (metadata.get("source_artifact_id") != source.id
                    or metadata.get("source_content_hash") != source_hash
                    or metadata.get("preview_version") != PREVIEW_VERSION):
                continue
            try:
                path = _managed_file(self.paths, preview.relative_path)
                if path.stat().st_size > 0:
                    return preview
            except (OSError, ValueError):
                continue
        return None

    @staticmethod
    def _ready(preview: Artifact) -> dict[str, Any]:
        return {"status": "ready", "artifact_id": preview.id,
                "content_url": f"/api/v1/artifacts/{preview.id}/content", "mime_type": "video/mp4"}

    def request(self, artifact_id: str) -> tuple[dict[str, Any], int]:
        source, path = self._source(artifact_id)
        source_hash = source.content_hash or _source_hash(path)
        probe_video(path)
        with self.database.immediate_session() as session:
            current = self._writable_source(session, source, source_hash)
            preview = self._cached(session, current, source_hash)
            if preview is not None:
                return self._ready(preview), 200
            for job in session.scalars(select(Job).where(
                Job.kind == "video.preview", Job.status.in_(("queued", "running", "cancel_requested")))
                .order_by(Job.created_at.asc(), Job.id.asc())).all():
                payload = job.payload_json or {}
                if (payload.get("source_artifact_id") == source.id
                        and payload.get("source_content_hash") == source_hash
                        and payload.get("preview_version") == PREVIEW_VERSION):
                    return {"status": job.status, "job_id": job.id}, 202
            job = self.jobs.enqueue_in_session(
                session, "video.preview",
                {"source_artifact_id": source.id, "source_content_hash": source_hash,
                 "preview_version": PREVIEW_VERSION},
                session_id=source.session_id, resource_keys=[f"artifact:video-preview:{source.id}"],
            )
            return {"status": job.status, "job_id": job.id}, 202

    def generate(self, payload: dict[str, Any], progress: Callable[[float, str | None], None], cancel_event: threading.Event) -> dict[str, str]:
        _check_cancel(cancel_event)
        source, source_path = self._source(str(payload.get("source_artifact_id") or ""))
        source_hash = source.content_hash or _source_hash(source_path, cancel_event)
        if (payload.get("source_content_hash", source_hash) != source_hash
                or payload.get("preview_version", PREVIEW_VERSION) != PREVIEW_VERSION):
            raise ValueError("The video preview request no longer matches its source or preview version.")
        info = probe_video(source_path, cancel_event)
        with self.database.session() as session:
            current = self._writable_source(session, source, source_hash)
            cached = self._cached(session, current, source_hash)
            if cached is not None:
                return {"artifact_id": cached.id, "source_artifact_id": source.id}
            if source.session_id:
                owner = session.get(SessionRecord, source.session_id)
                if owner is None or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", owner.storage_key):
                    raise ValueError("The session has no safe managed preview directory.")
                directory = self.paths.sessions / owner.storage_key / "video-previews"
            else:
                directory = self.paths.artifacts / "video-previews"
        for ancestor in [directory, *directory.parents]:
            if ancestor == self.paths.root:
                break
            if ancestor.is_symlink():
                raise ValueError("The preview directory contains an unsafe managed path.")
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"video-preview-{new_id()}.mp4"
        temporary = directory / f".video-preview-{new_id()}.mp4"
        published = False
        try:
            command = [resolve_ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-y",
                       "-i", str(source_path), "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
                       "-map_metadata", "-1", "-map_chapters", "-1", "-vf",
                       "scale=w='max(2,trunc(iw*sar*min(1,720/ih)/2)*2)':"
                       "h='max(2,trunc(ih*min(1,720/ih)/2)*2)',setsar=1",
                       "-r", str(min(30.0, info.frame_rate or 30.0)), "-c:v", "libx264",
                       "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac",
                       "-b:a", "128k", "-movflags", "+faststart", "-fs", str(MAX_OUTPUT_BYTES),
                       "-progress", "pipe:1", "-nostats", str(temporary)]
            progress(0.05, "Preparing compatible video preview")

            def report(record: dict[str, str]) -> None:
                elapsed = _finite_positive(record.get("out_time_us")) / 1_000_000
                progress(min(0.85, 0.05 + 0.8 * elapsed / info.duration_seconds), "Transcoding video preview")

            run_media_process(command, cancel_event=cancel_event, progress_callback=report,
                              timeout_seconds=PROCESS_TIMEOUT_SECONDS)
            _check_cancel(cancel_event)
            if not temporary.is_file() or not 0 < temporary.stat().st_size <= MAX_OUTPUT_BYTES:
                raise MediaProcessError("Video preview exceeded its output size bound or produced an empty file.")
            output = probe_video(temporary, cancel_event)
            if (abs(output.duration_seconds - info.duration_seconds) > max(0.25, info.duration_seconds * 0.01)
                    or output.height > 720 or output.width % 2 or output.height % 2
                    or output.video_codec != "h264" or output.pixel_format != "yuv420p"
                    or not 0 < output.frame_rate <= 30.001
                    or (info.has_audio and (not output.has_audio or output.audio_codec != "aac"))):
                raise MediaProcessError("Video preview is incomplete or does not meet the compatible media bounds.")
            if _source_hash(source_path, cancel_event) != source_hash:
                raise ValueError("The video source changed while its preview was being prepared.")
            prepared = replace(self.artifacts.prepare_registration(temporary),
                               relative_path=destination.relative_to(self.paths.root).as_posix())
            progress(0.9, "Registering compatible video preview")
            _check_cancel(cancel_event)
            with self.database.immediate_session() as session:
                current = self._writable_source(session, source, source_hash)
                _check_cancel(cancel_event)
                cached = self._cached(session, current, source_hash)
                if cached is not None:
                    return {"artifact_id": cached.id, "source_artifact_id": source.id}
                os.replace(temporary, destination)
                published = True
                preview = self.artifacts.register_in_session(
                    session, destination, kind="video", role=PREVIEW_ROLE,
                    session_id=current.session_id, parent_ids=[current.id], _prepared=prepared,
                    metadata={"source_artifact_id": current.id, "source_content_hash": source_hash,
                              "source_artifact_hash": source_hash, "preview_version": PREVIEW_VERSION,
                              "duration_ms": round(output.duration_seconds * 1000)},
                )
                result = {"artifact_id": preview.id, "source_artifact_id": current.id}
            published = False
            return result
        finally:
            temporary.unlink(missing_ok=True)
            if published:
                destination.unlink(missing_ok=True)
