import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import SCHEMA_HEAD, Database, upgrade_database
from pandrator.web.media_edit import (
    MediaEditInputsChanged,
    MediaEditRevisionConflict,
    MediaEditService,
)
from pandrator.web.models import Artifact, SessionRecord, SessionSource, SourceAsset


class MediaEditServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.directory.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.session_id = "11111111-1111-4111-8111-111111111111"
        with self.database.session() as session:
            session.add(
                SessionRecord(
                    id=self.session_id,
                    name="Media edit",
                    storage_key="22222222-2222-4222-8222-222222222222",
                    workflow_kind="media_edit",
                )
            )

    def tearDown(self):
        self.database.dispose()
        self.directory.cleanup()

    def _register(
        self,
        name: str,
        role: str,
        content: str | bytes,
        kind: str,
        *,
        parent_ids: list[str] | None = None,
        metadata: dict | None = None,
    ):
        path = self.paths.root / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return self.artifacts.register(
            path,
            kind=kind,
            role=role,
            session_id=self.session_id,
            parent_ids=parent_ids,
            metadata=metadata,
        )

    def _attach(self, artifact, role: str, kind: str):
        with self.database.session() as session:
            asset = SourceAsset(
                artifact_id=artifact.id,
                display_name=artifact.relative_path,
                kind=kind,
                state="current",
            )
            session.add(asset)
            session.flush()
            session.add(
                SessionSource(
                    session_id=self.session_id,
                    source_asset_id=asset.id,
                    role=role,
                    is_current=True,
                )
            )

    def _service(self):
        return MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.paths.sessions,
            duration_probe=lambda _path: 5000,
        )

    def _seed_external(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAlice: hello world\n\n00:00:01.800 --> 00:00:02.800\nBob: overlap\n",
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")
        timing = self._register(
            "timing.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "timed",
                            "start_ms": 2000,
                            "end_ms": 3800,
                            "text": "hello world overlap",
                            "words": [
                                {"text": "hello", "start_ms": 2000, "end_ms": 2400},
                                {"text": "world", "start_ms": 2500, "end_ms": 2900},
                                {"text": "overlap", "start_ms": 3000, "end_ms": 3800},
                            ],
                        }
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
        )
        return media, captions, timing

    def test_schema_head_and_media_edit_foreign_keys(self):
        with sqlite3.connect(self.paths.database) as connection:
            self.assertEqual(
                SCHEMA_HEAD,
                connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0],
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("media_edit_plans", tables)
            self.assertIn("media_edit_plan_revisions", tables)
            self.assertEqual(
                [], connection.execute("PRAGMA foreign_key_check").fetchall()
            )

    def test_external_prepare_aligns_constant_offset_and_preserves_speakers(self):
        self._seed_external()
        service = self._service()
        result = service.prepare(self.session_id)
        plan = result["plan"]
        self.assertEqual(1, plan["revision"])
        self.assertEqual(
            {"id": "keep-000001", "start_ms": 0, "end_ms": 5000, "label": None},
            plan["keep_ranges"][0],
        )
        self.assertEqual(2000, plan["cues"][0]["start_ms"])
        self.assertEqual("Alice", plan["cues"][0]["speaker"])
        self.assertEqual("Bob", plan["cues"][1]["speaker"])
        self.assertEqual("external_transcript", plan["evidence"]["source_kind"])
        self.assertEqual(
            {
                "ready",
                "source_media_artifact",
                "external_transcript_artifact",
                "transcription_artifact",
                "timing_artifact",
            },
            set(result["readiness"]),
        )
        self.assertEqual(set(result), set(service.state(self.session_id)))

    def test_asr_only_prepare_is_supported_and_repeated_prepare_is_idempotent(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        transcript = self._register(
            "transcription.srt",
            "transcription",
            "1\n00:00:01,000 --> 00:00:02,000\nhello\n",
            "srt",
            parent_ids=[media.id],
        )
        service = self._service()
        first = service.prepare(self.session_id)
        second = service.prepare(self.session_id)
        self.assertEqual(1, first["plan"]["revision"])
        self.assertEqual(first["plan"]["revision_id"], second["plan"]["revision_id"])
        self.assertEqual(
            transcript.id, first["plan"]["editorial_transcript_artifact"]["id"]
        )

    def test_prepare_ignores_timing_from_a_different_primary_source(self):
        old_media = self._register("old.mp4", "upload", b"old", "video")
        timing = self._register(
            "old-timing.json",
            "word_timestamps",
            json.dumps({"schema": "pandrator.transcript.v1", "segments": []}),
            "json",
            parent_ids=[old_media.id],
        )
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAlice: hello\n",
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")

        state = self._service().prepare(self.session_id)

        self.assertIsNone(state["plan"]["timing_artifact"])
        self.assertIsNone(state["readiness"]["timing_artifact"])
        self.assertNotEqual(timing.id, state["plan"].get("timing_artifact_id"))

    def test_update_is_immutable_and_normalizes_ranges(self):
        self._seed_external()
        service = self._service()
        first = service.prepare(self.session_id)
        second = service.update(
            self.session_id,
            1,
            keep_ranges=[
                {"start_ms": 100, "end_ms": 2000},
                {"start_ms": 1800, "end_ms": 5000},
            ],
            instructions="Keep the dialogue.",
            reviewed=True,
        )
        self.assertEqual(2, second["plan"]["revision"])
        self.assertEqual(
            first["plan"]["revision_id"], second["plan"]["parent_revision_id"]
        )
        self.assertEqual(100, second["plan"]["keep_ranges"][0]["start_ms"])
        self.assertEqual(5000, second["plan"]["keep_ranges"][0]["end_ms"])
        self.assertEqual(set(first), set(second))
        with self.assertRaises(MediaEditRevisionConflict):
            service.update(
                self.session_id, 1, keep_ranges=[{"start_ms": 0, "end_ms": 1}]
            )
        self.assertEqual(1, service.revision(self.session_id, 1)["revision"])

    def test_noop_update_reuses_the_active_revision(self):
        self._seed_external()
        service = self._service()
        first = service.prepare(self.session_id)

        second = service.update(
            self.session_id,
            first["plan"]["revision"],
            keep_ranges=first["plan"]["keep_ranges"],
            instructions=first["plan"]["instructions"],
            reviewed=first["plan"]["reviewed"],
        )

        self.assertEqual(first["plan"]["revision_id"], second["plan"]["revision_id"])

    def test_content_change_does_not_inherit_reviewed_state(self):
        self._seed_external()
        service = self._service()
        prepared = service.prepare(self.session_id)
        reviewed = service.update(
            self.session_id,
            prepared["plan"]["revision"],
            keep_ranges=prepared["plan"]["keep_ranges"],
            reviewed=True,
        )

        changed = service.update(
            self.session_id,
            reviewed["plan"]["revision"],
            keep_ranges=reviewed["plan"]["keep_ranges"],
            instructions="Remove the private discussion.",
        )

        self.assertFalse(changed["plan"]["reviewed"])

    def test_new_revision_invalidates_rendered_outputs_and_descendants(self):
        self._seed_external()
        service = self._service()
        prepared = service.prepare(self.session_id)
        revision = prepared["plan"]
        metadata = {
            "revision_id": revision["revision_id"],
            "content_hash": revision["content_hash"],
        }
        media = self._register(
            "edited.mp4",
            "media_edit_media",
            b"edited",
            "video",
            metadata=metadata,
        )
        subtitles = self._register(
            "edited.srt",
            "media_edit_subtitles",
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            "srt",
            parent_ids=[media.id],
            metadata=metadata,
        )
        correction = self._register(
            "corrected.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
            "srt",
            parent_ids=[subtitles.id],
        )

        service.update(
            self.session_id,
            revision["revision"],
            keep_ranges=revision["keep_ranges"],
            instructions="A changed edit decision.",
        )

        with self.database.session() as session:
            self.assertEqual("stale", session.get(Artifact, media.id).state)
            self.assertEqual("stale", session.get(Artifact, subtitles.id).state)
            self.assertEqual("stale", session.get(Artifact, correction.id).state)

    def test_proposal_preserves_caption_boundary_without_reliable_asr_alignment(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAlice: remove this\n",
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")
        service = self._service()
        service.prepare(self.session_id)

        result = service.apply_proposal(
            self.session_id,
            1,
            [
                {
                    "start_cue_id": "cue-000001",
                    "end_cue_id": "cue-000001",
                    "reason": "Remove setup chatter.",
                }
            ],
        )

        cut = result["plan"]["operation"]["cuts"][0]
        self.assertEqual((1000, 2000), (cut["start_ms"], cut["end_ms"]))
        self.assertEqual("caption_boundary", cut["start"]["method"])
        self.assertEqual("caption_boundary", cut["end"]["method"])
        self.assertTrue(cut["start"]["warnings"])
        self.assertIn(
            "No ASR timing was available",
            result["plan"]["evidence"]["warnings"][0],
        )

    def test_prepare_does_not_hold_immediate_write_session_during_probe(self):
        self._seed_external()
        immediate_entries: list[str] = []
        original_immediate_session = self.database.immediate_session

        @contextmanager
        def instrumented_immediate_session():
            immediate_entries.append("entered")
            with original_immediate_session() as session:
                yield session

        def probe(_path):
            self.assertEqual([], immediate_entries)
            return 5000

        service = MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.paths.sessions,
            duration_probe=probe,
        )
        with patch.object(
            self.database, "immediate_session", instrumented_immediate_session
        ):
            service.prepare(self.session_id)
        self.assertEqual(["entered"], immediate_entries)

    def test_prepare_rejects_inputs_changed_after_read_snapshot(self):
        media, _captions, _timing = self._seed_external()

        def probe(_path):
            with self.database.session() as session:
                current = session.get(Artifact, media.id)
                current.content_hash = "changed-during-prepare"
            return 5000

        service = MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.paths.sessions,
            duration_probe=probe,
        )
        with self.assertRaises(MediaEditInputsChanged):
            service.prepare(self.session_id)


if __name__ == "__main__":
    unittest.main()
