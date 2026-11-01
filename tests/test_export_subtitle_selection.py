"""Queued display exports retain exact selected subtitle track identities."""

from __future__ import annotations

import threading
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from sqlalchemy import event, func, select

from pandrator.web.artifact_selection import ROLE_TO_STAGE, choose_artifact, selected_artifacts
from pandrator.web.export_inputs import resolve_export_inputs, select_media_export
from pandrator.web.export_subtitle_selection import (
    SUBTITLE_TRACK_ROLES,
    capture_subtitle_track_snapshot,
    collect_export_roles,
)
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    AudioTake,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    SessionStageSelection,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflows import WorkflowService
from tests import test_generation_display_subtitles as fixtures


class ExportSubtitleSelectionTests(unittest.TestCase):
    setUp = fixtures.SnapshotAndExportTests.setUp
    _artifact = fixtures.SnapshotAndExportTests._artifact
    _revision = fixtures.SnapshotAndExportTests._revision
    _snapshot = fixtures.SnapshotAndExportTests._snapshot
    _run = fixtures.SnapshotAndExportTests._run

    def _queue(self, **settings):
        queue = JobQueue(self.database)
        job = WorkflowService(self.database, queue).run_stage(
            self.record.id,
            "export",
            {"export_mode": "subtitles", **settings},
        )
        return deepcopy(queue.get(job.id).payload_json)

    def _export(self, payload):
        result = self.handlers.export(payload, lambda *_: None, threading.Event())
        return {
            artifact.role: (artifact, path.read_text(encoding="utf-8"))
            for artifact, path in (self.artifacts.resolve(item) for item in result["artifact_ids"])
        }

    def _select(self, stage, artifact_id):
        with self.database.session() as session:
            choose_artifact(session, self.record.id, stage, artifact_id)

    def _speech_counts(self):
        with self.database.session() as session:
            return tuple(
                session.scalar(select(func.count()).select_from(model))
                for model in (
                    GenerationPlanRevision,
                    GenerationSegment,
                    GenerationRun,
                    AudioTake,
                )
            )

    def _legacy_role_ids(self):
        # Reference deliberately remains outside the bounded-capture load spy.
        with self.database.session() as session:
            current = list(
                session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.session_id == self.record.id,
                        Artifact.state == "current",
                    )
                    .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                ).all()
            )
            by_role = collect_export_roles(current, selected_artifacts(session, self.record.id))
            return {role: by_role[role].id for role in SUBTITLE_TRACK_ROLES if role in by_role}

    def test_historical_text_never_silently_exports_a_different_subtitle_master(self):
        revision_id = self._revision("Historical reviewed wording")
        run_id = self._run(revision_id)
        self._artifact(
            "different-master.srt", fixtures.SOURCE_SRT.replace("First cue", "Different master"),
            "correction",
        )
        payload = self._queue(
            generation_run_id=run_id,
            subtitle_text_source="generation_run",
            subtitle_selection="source",
        )
        counts = self._speech_counts()
        with self.assertRaisesRegex(ValueError, "historical text.*different subtitle source"):
            self._export(payload)
        self.assertEqual(counts, self._speech_counts())
        with self.database.session() as session:
            self.assertEqual(0, session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.role.startswith("export"))
            ))

    def test_bounded_fallback_matches_explicit_stale_none_and_current_siblings(self):
        stale = self._artifact("selected-stale.srt", fixtures.SOURCE_SRT, "correction")
        self._artifact("current-correction.srt", fixtures.SOURCE_SRT, "correction")
        translation = self._artifact("explicit-none.srt", fixtures.SOURCE_SRT, "translation")
        older = self._artifact("older-edit.srt", fixtures.SOURCE_SRT, "media_edit_subtitles")
        newer = self._artifact("newer-edit.srt", fixtures.SOURCE_SRT, "media_edit_subtitles")
        sibling = self._artifact("stage-sibling.srt", fixtures.SOURCE_SRT, "editorial_edit_variant")
        with self.database.session() as session:
            for row in session.scalars(
                select(SessionStageSelection).where(
                    SessionStageSelection.session_id == self.record.id,
                )
            ).all():
                session.delete(row)
            session.add_all(
                [
                    SessionStageSelection(
                        session_id=self.record.id, stage_key="correct", artifact_id=stale.id
                    ),
                    SessionStageSelection(
                        session_id=self.record.id, stage_key="translate", artifact_id=None
                    ),
                ]
            )
            origin = datetime(2026, 1, 1, tzinfo=UTC)
            for offset, artifact in enumerate((sibling, older, newer)):
                row = session.get(Artifact, artifact.id)
                row.state = "current"
                row.created_at = origin + timedelta(seconds=offset)
            session.get(Artifact, stale.id).state = "stale"
        with patch.dict(ROLE_TO_STAGE, {"editorial_edit_variant": "edit_media"}):
            expected = self._legacy_role_ids()
            with self.database.session() as session:
                snapshot = capture_subtitle_track_snapshot(session, self.record.id)
        actual = {track["role"]: track["artifact_id"] for track in snapshot["tracks"]}
        self.assertEqual(expected, actual)
        self.assertEqual(stale.id, actual["correction"])
        # Explicit None suppresses selector overrides, while the legacy worker
        # still retains its current role map; preserve that exact behavior.
        self.assertEqual(translation.id, actual["translation"])
        self.assertEqual(newer.id, actual["media_edit_subtitles"])

    def test_capture_never_loads_unrelated_current_or_historical_take_artifacts(self):
        revision_id = self._revision("Unrelated take plan")
        sentinels = [
            self._artifact(
                f"take-sentinel-{index}.srt",
                fixtures.SOURCE_SRT,
                "generation_take",
                {"sentinel_metadata": "x" * 16384},
            )
            for index in range(8)
        ]
        sentinel_ids = {artifact.id for artifact in sentinels}
        with self.database.session() as session:
            segment_id = session.scalar(
                select(GenerationSegment.id).where(
                    GenerationSegment.plan_revision_id == revision_id
                )
            )
            for index, artifact in enumerate(sentinels):
                row = session.get(Artifact, artifact.id)
                row.state = "stale" if index % 2 else "current"
                session.add(
                    AudioTake(
                        generation_segment_id=segment_id,
                        artifact_id=artifact.id,
                        status="completed",
                        kind="tts",
                    )
                )
        expected = self._legacy_role_ids()
        loaded = []

        def reject_take_load(artifact, _context):
            self.assertNotIn(artifact.id, sentinel_ids, "Capture loaded unrelated take history")
            loaded.append(artifact.id)

        event.listen(Artifact, "load", reject_take_load)
        try:
            with self.database.session() as session:
                snapshot = capture_subtitle_track_snapshot(session, self.record.id)
        finally:
            event.remove(Artifact, "load", reject_take_load)
        self.assertIn(self.display.id, loaded)
        self.assertEqual(
            expected, {track["role"]: track["artifact_id"] for track in snapshot["tracks"]}
        )

    def test_capture_matches_legacy_worker_roles_and_preserves_nullable_metadata(self):
        for role in ("media_edit_subtitles", "correction", "translation"):
            self._artifact(f"capture-{role}.srt", fixtures.SOURCE_SRT, role)
        with self.database.session() as session:
            display = session.get(Artifact, self.display.id)
            display.content_hash = None
            display.metadata_json = {"revision_id": None}
        payload = self._queue(subtitle_selection="dual")
        tracks = payload["subtitle_track_snapshot"]["tracks"]
        self.assertEqual(
            ["correction", "media_edit_subtitles", "transcription", "translation"],
            [track["role"] for track in tracks],
        )
        transcription = next(track for track in tracks if track["role"] == "transcription")
        self.assertIsNone(transcription["content_hash"])
        self.assertIsNone(transcription["revision_id"])
        legacy = {key: value for key, value in payload.items() if key != "subtitle_track_snapshot"}
        inputs = resolve_export_inputs(self.handlers._output_workflow_context(), legacy)
        self.assertEqual(
            {track["role"]: track["artifact_id"] for track in tracks},
            {
                role: inputs.by_role[role].id
                for role in (
                    "correction",
                    "media_edit_subtitles",
                    "transcription",
                    "translation",
                )
            },
        )

    def test_queued_correction_survives_new_head_and_selection_with_frozen_display(self):
        correction = self._artifact("queued-correction.srt", fixtures.SOURCE_SRT, "correction")
        self._select("correct", correction.id)
        self._revision("Frozen reviewed wording", source=correction)
        payload = self._queue(subtitle_selection="source")
        pins = {row["role"]: row for row in payload["subtitle_track_snapshot"]["tracks"]}
        self.assertEqual(correction.id, pins["correction"]["artifact_id"])
        self.assertEqual(correction.content_hash, pins["correction"]["content_hash"])
        self.assertIsNone(pins["correction"]["revision_id"])
        later = self._artifact(
            "later-correction.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Later correction"),
            "correction",
        )
        self._select("correct", later.id)
        self._revision("Later plan wording", source=later)
        before = self._speech_counts()
        outputs = self._export(payload)
        artifact, text = outputs["export_subtitle_source"]
        self.assertIn("Frozen reviewed wording", text)
        self.assertNotIn("Later correction", text)
        self.assertNotIn("Later plan wording", text)
        self.assertEqual(before, self._speech_counts())
        with self.database.session() as session:
            parent_id = session.scalar(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == artifact.id
                )
            )
            derived = session.get(Artifact, parent_id)
            self.assertEqual(correction.id, derived.metadata_json["original_parent_id"])
            self.assertEqual(
                {correction.id},
                set(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == derived.id
                        )
                    )
                ),
            )

    def test_dual_tracks_keep_both_queued_identities_and_separate_projection(self):
        correction = self._artifact("source-track.srt", fixtures.SOURCE_SRT, "correction")
        translation = self._artifact(
            "target-track.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Queued German wording"),
            "translation",
            {"language": "de"},
        )
        self._revision("Queued source display", source=correction)
        payload = self._queue(subtitle_selection="dual")
        later_source = self._artifact(
            "late-source.srt", fixtures.SOURCE_SRT.replace("First cue", "Late source"), "correction"
        )
        later_target = self._artifact(
            "late-target.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Late target"),
            "translation",
        )
        self._select("correct", later_source.id)
        self._select("translate", later_target.id)
        outputs = self._export(payload)
        self.assertEqual(2, len(outputs))
        self.assertIn("Queued source display", outputs["export_subtitle_source"][1])
        target, text = outputs["export_subtitle_translation"]
        self.assertIn("Queued German wording", text)
        self.assertNotIn("Queued source display", text)
        self.assertNotIn("Late target", text)
        with self.database.session() as session:
            self.assertEqual(
                {translation.id},
                set(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == target.id
                        )
                    )
                ),
            )

    def test_missing_translation_stays_missing_after_late_arrival(self):
        payload = self._queue(subtitle_selection="auto")
        self.assertNotIn(
            "translation", {row["role"] for row in payload["subtitle_track_snapshot"]["tracks"]}
        )
        self._artifact(
            "late-translation.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Late translation"),
            "translation",
        )
        outputs = self._export(payload)
        self.assertEqual({"export_subtitle_source"}, set(outputs))
        self.assertNotIn("Late translation", outputs["export_subtitle_source"][1])

    def test_empty_role_snapshot_retains_pinned_attached_srt_only(self):
        self.record = SessionService(self.database).create(
            "Attached subtitle only", workflow_kind="subtitles"
        )
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir()
        upload = self._artifact("attached.srt", fixtures.SOURCE_SRT, "upload")
        payload = self._queue(subtitle_selection="auto")
        self.assertEqual({"version": 1, "tracks": []}, payload["subtitle_track_snapshot"])
        self.assertEqual(upload.id, payload["export_contract"]["source_artifact_id"])
        self._artifact(
            "late-correction.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Late correction"),
            "correction",
        )
        self._artifact(
            "late-translation.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Late translation"),
            "translation",
        )
        self._artifact(
            "late-upload.srt", fixtures.SOURCE_SRT.replace("First cue", "Late upload"), "upload"
        )
        outputs = self._export(payload)
        self.assertEqual({"export_subtitle_source"}, set(outputs))
        artifact, text = outputs["export_subtitle_source"]
        self.assertIn("First cue", text)
        self.assertNotIn("Late", text)
        with self.database.session() as session:
            self.assertEqual(
                {upload.id},
                set(
                    session.scalars(
                        select(ArtifactEdge.parent_artifact_id).where(
                            ArtifactEdge.child_artifact_id == artifact.id
                        )
                    )
                ),
            )

    def test_absent_snapshot_retains_legacy_selection_without_new_projection(self):
        self._revision("Old display wording")
        payload = self._queue(subtitle_selection="source")
        del payload["subtitle_track_snapshot"]
        newer = self._artifact(
            "legacy-later-correction.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Legacy current correction"),
            "correction",
        )
        self._select("correct", newer.id)
        outputs = self._export(payload)
        self.assertIn("Legacy current correction", outputs["export_subtitle_source"][1])
        self.assertNotIn("Old display wording", outputs["export_subtitle_source"][1])

    def test_current_and_historical_display_modes_require_their_matching_master(self):
        old_revision = self._revision("Historical display")
        run_id = self._run(old_revision)
        self._revision("Current display")
        for mode, expected in (
            ("current", "Current display"),
            ("generation_run", "Historical display"),
        ):
            with self.subTest(mode=mode):
                payload = self._queue(
                    subtitle_selection="source", generation_run_id=run_id, subtitle_text_source=mode
                )
                outputs = self._export(payload)
                self.assertIn(expected, outputs["export_subtitle_source"][1])
        # An explicit historical choice must not silently become another master.
        selected = self._artifact(
            "selected-history.srt",
            fixtures.SOURCE_SRT.replace("First cue", "Selected history body"),
            "correction",
        )
        self._select("correct", selected.id)
        payload = self._queue(
            subtitle_selection="source",
            generation_run_id=run_id,
            subtitle_text_source="generation_run",
        )
        with self.assertRaisesRegex(ValueError, "historical text.*different subtitle source"):
            self._export(payload)
        payload = self._queue(
            subtitle_selection="source",
            generation_run_id=run_id,
            subtitle_text_source="current",
        )
        text = self._export(payload)["export_subtitle_source"][1]
        self.assertIn("Selected history body", text)
        self.assertNotIn("Historical display", text)

    def test_output_context_identifies_the_same_masters_as_submission(self):
        from pandrator.web.workspace_settings import WorkspaceSettingsService

        revision_id = self._revision("Current display")
        context = WorkspaceSettingsService(self.database).get(self.record.id, "output")["context"]
        payload = self._queue(subtitle_selection="source")
        self.assertEqual(context["subtitle_tracks"], payload["subtitle_track_snapshot"]["tracks"])
        self.assertEqual(
            {"artifact_id": self.display.id, "revision_id": revision_id},
            context["subtitle_display_revision"],
        )

    def test_invalid_pins_fail_before_file_resolution_or_publication(self):
        for change in ("hash", "revision", "role", "deleted", "missing", "foreign"):
            with self.subTest(change=change):
                source = self._artifact(f"guard-{change}.srt", fixtures.SOURCE_SRT, "correction")
                payload = self._queue(subtitle_selection="source")
                pin = next(
                    track
                    for track in payload["subtitle_track_snapshot"]["tracks"]
                    if track["role"] == "correction"
                )
                restore = None
                if change == "foreign":
                    foreign = SessionService(self.database).create(
                        "Foreign", workflow_kind="subtitles"
                    )
                    path = self.paths.sessions / foreign.storage_key / "foreign.srt"
                    path.parent.mkdir()
                    path.write_text(fixtures.SOURCE_SRT, encoding="utf-8")
                    artifact = self.artifacts.register(
                        path, kind="srt", role=pin["role"], session_id=foreign.id
                    )
                    pin["artifact_id"] = artifact.id
                else:
                    with self.database.session() as session:
                        artifact = session.get(Artifact, source.id)
                        restore = {
                            key: deepcopy(getattr(artifact, key))
                            for key in (
                                "content_hash",
                                "metadata_json",
                                "role",
                                "state",
                            )
                        }
                        if change == "hash":
                            artifact.content_hash = "changed"
                        elif change == "revision":
                            artifact.metadata_json = {
                                **artifact.metadata_json,
                                "revision_id": "changed",
                            }
                        elif change == "role":
                            artifact.role = "translation"
                        elif change == "deleted":
                            artifact.state = "deleted"
                        elif change == "missing":
                            session.delete(artifact)
                with (
                    patch.object(self.handlers, "_resolve_input") as read,
                    patch.object(self.artifacts, "register") as publish,
                ):
                    with self.assertRaisesRegex(ValueError, "subtitle track"):
                        self.handlers.export(payload, lambda *_: None, threading.Event())
                    read.assert_not_called()
                    publish.assert_not_called()
                self.assertFalse((self.session_dir / "exports").exists())
                if restore is not None and change != "missing":
                    with self.database.session() as session:
                        artifact = session.get(Artifact, source.id)
                        for key, value in restore.items():
                            setattr(artifact, key, value)

    def test_malformed_present_snapshot_never_falls_back(self):
        payload = self._queue()
        track = deepcopy(payload["subtitle_track_snapshot"]["tracks"][0])
        malformed = [
            None,
            [],
            {},
            {"version": 2, "tracks": []},
            {"version": True, "tracks": []},
            {"version": 1, "tracks": None},
            {"version": 1, "tracks": [track, track]},
            {"version": 1, "tracks": [{**track, "role": "upload"}]},
            {"version": 1, "tracks": [{**track, "artifact_id": ""}]},
            {"version": 1, "tracks": [{**track, "content_hash": 123}]},
            {"version": 1, "tracks": [{"role": "correction"}]},
            {"version": 1, "tracks": [track, {**track, "role": "translation"}]},
            {"version": 1, "tracks": [{**track, "revision_id": []}]},
        ]
        for snapshot in malformed:
            with self.subTest(snapshot=snapshot):
                with (
                    patch.object(self.handlers, "_resolve_input") as read,
                    patch.object(self.artifacts, "register") as publish,
                ):
                    with self.assertRaisesRegex(ValueError, "snapshot is malformed"):
                        self.handlers.export(
                            {**payload, "subtitle_track_snapshot": snapshot},
                            lambda *_: None,
                            threading.Event(),
                        )
                    read.assert_not_called()
                    publish.assert_not_called()
        self.assertFalse((self.session_dir / "exports").exists())

    def test_exact_pins_are_authorized_before_they_can_be_read(self):
        payload = self._queue()
        guard = Mock()
        authorize = Mock(side_effect=PermissionError("Revoked exact input"))
        context = replace(
            self.handlers._output_workflow_context(),
            _authorization_guard=guard,
            _authorize_input=authorize,
        )
        with self.assertRaisesRegex(PermissionError, "Revoked"):
            resolve_export_inputs(context, payload)
        guard.assert_called_once()
        self.assertEqual(self.display.id, authorize.call_args.args[1])
        authorize.side_effect = None
        inputs = resolve_export_inputs(context, payload)
        self.assertEqual(
            [self.display.id], [item.id for item in select_media_export(inputs).selected_subtitles]
        )
