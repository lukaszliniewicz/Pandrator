"""Source reset safety and explicit preparation/review/selection regression tests."""

import io
import tempfile
import threading
import unittest
import uuid
import wave
from unittest.mock import patch

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web import models as m

SRT = "1\n00:00:01,000 --> 00:00:03,000\nHello, world.\n\n2\n00:00:04,000 --> 00:00:06,000\nA second complete thought.\n"


class SessionSourcePlanControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        self.services = self.app.extensions["pandrator"]["services"]
        self.sid = self.client.post(
            "/api/v1/sessions",
            json={"name": "Source and plan controls", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        self.uploaded = self.upload(SRT.encode(), "original.srt", self.sid)
        self.base = f"/api/v1/sessions/{self.sid}"
        with self.services.database.session() as session:
            outcome = session.get(m.OutcomePlan, self.sid)
            if outcome is None:
                outcome = m.OutcomePlan(session_id=self.sid, value_json={})
                session.add(outcome)
            outcome.value_json = {
                **dict(outcome.value_json or {}),
                "inputs": {"generation": "source"},
            }

    def upload(self, data, filename, sid=None):
        fields = {"file": (io.BytesIO(data), filename)}
        if sid:
            fields["session_id"] = sid
        response = self.client.post(
            "/api/v1/uploads", data=fields, headers=self.headers
        )
        self.assertEqual(response.status_code, 201, response.get_json())
        return response.get_json()

    @staticmethod
    def wav_data(value):
        output = io.BytesIO()
        with wave.open(output, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(int(value).to_bytes(2, "little", signed=True) * 1600)
        return output.getvalue()

    def post(self, suffix, data, key=None):
        return self.client.post(
            self.base + suffix,
            json=data,
            headers={**self.headers, "Idempotency-Key": key or uuid.uuid4().hex},
        )

    def change(self, source_id, role="primary", preview=None, key=None):
        values = {"role": role, "new_source_asset_id": source_id}
        if preview is None:
            response = self.post("/sources/change-preview", values)
            self.assertEqual(response.status_code, 200, response.get_json())
            preview = response.get_json()
        return self.post(
            "/sources/change",
            {
                **values,
                "expected_revision": preview["session_revision"],
                "impact_token": preview["impact_token"],
            },
            key,
        )

    def prepare(self):
        status = self.client.get(self.base + "/generation-plan/status")
        self.assertEqual(status.status_code, 200, status.get_json())
        state = status.get_json()
        self.assertIsNotNone(state["current_input"], state)
        response = self.post(
            "/generation-plan/prepare",
            {
                "expected_revision": state["session_revision"],
                "expected_plan_revision_id": state["selected_revision_id"],
                "source_artifact_id": state["current_input"]["artifact_id"],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_reset_removes_derived_state_and_files_but_retains_source_library(self):
        plan = self.prepare()
        p = self.services.paths.uploads / "derived.bin"
        p.write_bytes(b"derived-output")
        artifact = self.services.artifacts.register(
            p, kind="audio", role="assembled_audio", session_id=self.sid
        )
        replacement = self.upload(
            SRT.replace("Hello", "Goodbye").encode(), "replacement.srt"
        )
        response = self.change(replacement["source_asset_id"])
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertFalse(p.exists())
        with self.services.database.session() as session:
            self.assertIsNone(
                session.get(m.GenerationPlanRevision, plan["selected_revision_id"])
            )
            old = session.get(m.Artifact, self.uploaded["artifact_id"])
            self.assertIsNone(old.session_id)
            self.assertNotEqual(old.state, "deleted")
            self.assertEqual(session.get(m.Artifact, artifact.id).state, "deleted")
            self.assertEqual(
                session.scalar(
                    select(func.count())
                    .select_from(m.Document)
                    .where(m.Document.session_id == self.sid)
                ),
                1,
            )
        state = self.client.get(self.base + "/sources/status").get_json()
        self.assertEqual(
            state["primary"]["source_asset_id"], replacement["source_asset_id"]
        )
        self.assertFalse(state["subtitle"]["adoption_required"])

    def test_invalid_replacement_leaves_original_plan_and_source_untouched(self):
        plan = self.prepare()
        replacement = self.upload(b"invalid subtitle source", "broken.srt")
        response = self.change(replacement["source_asset_id"])
        self.assertEqual(response.status_code, 422, response.get_json())
        state = self.client.get(self.base + "/generation-plan/status").get_json()
        self.assertEqual(state["selected_revision_id"], plan["selected_revision_id"])
        self.assertEqual(
            self.client.get(self.base + "/sources/status").get_json()["primary"][
                "artifact_id"
            ],
            self.uploaded["artifact_id"],
        )

    def test_stale_reset_preview_and_active_work_are_refused(self):
        preview = self.post("/sources/change-preview", {"role": "primary"}).get_json()
        self.prepare()
        self.assertEqual(self.change(None, preview=preview).status_code, 409)
        with self.services.database.session() as session:
            session.add(m.Job(kind="noop", session_id=self.sid, status="queued"))
        self.assertEqual(self.change(None).status_code, 409)
        self.assertIsNotNone(
            self.client.get(self.base + "/sources/status").get_json()["primary"]
        )

    def test_reset_does_not_remove_artifact_used_by_another_session(self):
        other = self.client.post(
            "/api/v1/sessions",
            json={"name": "Other session", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        parent_path = self.services.paths.uploads / "shared-parent.txt"
        child_path = self.services.paths.uploads / "shared-child.txt"
        parent_path.write_text("shared text")
        child_path.write_text("other-session derivative")
        parent = self.services.artifacts.register(
            parent_path, kind="txt", role="correction", session_id=self.sid
        )
        child = self.services.artifacts.register(
            child_path,
            kind="txt",
            role="translation",
            session_id=other,
            parent_ids=[parent.id],
        )
        response = self.change(None)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(parent_path.exists())
        self.assertTrue(child_path.exists())
        with self.services.database.session() as session:
            self.assertIsNone(session.get(m.Artifact, parent.id).session_id)
            self.assertEqual(session.get(m.Artifact, child.id).session_id, other)

    def test_recording_replacement_keeps_text_and_plan_but_invalidates_assembly(self):
        plan = self.prepare()
        media = self.upload(self.wav_data(100), "recording.wav")
        self.assertEqual(
            self.change(media["source_asset_id"], "media").status_code, 200
        )
        p = self.services.paths.uploads / "assembly.wav"
        p.write_bytes(b"assembly")
        assembly = self.services.artifacts.register(
            p, kind="audio", role="assembled_audio", session_id=self.sid
        )
        new_media = self.upload(self.wav_data(200), "replacement.wav")
        changed = self.change(new_media["source_asset_id"], "media")
        self.assertEqual(changed.status_code, 200, changed.get_json())
        with self.services.database.session() as session:
            self.assertIsNotNone(
                session.get(m.GenerationPlanRevision, plan["selected_revision_id"])
            )
            self.assertEqual(session.get(m.Artifact, assembly.id).state, "stale")
        self.assertEqual(
            self.client.get(self.base + "/sources/status").get_json()["primary"][
                "artifact_id"
            ],
            self.uploaded["artifact_id"],
        )

    def test_prepare_is_separate_from_generation_and_selection_does_not_copy(self):
        first = self.prepare()
        second = self.prepare()
        with self.services.database.session() as session:
            self.assertEqual(
                session.scalar(select(func.count()).select_from(m.GenerationRun)), 0
            )
            self.assertEqual(session.scalar(select(func.count()).select_from(m.Job)), 0)
        selected = self.post(
            "/generation-plan/select",
            {
                "revision_id": first["selected_revision_id"],
                "expected_plan_revision_id": second["selected_revision_id"],
            },
        )
        self.assertEqual(selected.status_code, 200, selected.get_json())
        self.assertFalse(selected.get_json()["created_revision"])
        state = self.client.get(self.base + "/generation-plan/status").get_json()
        self.assertEqual(state["selected_revision_id"], first["selected_revision_id"])
        self.assertEqual(state["latest_revision_id"], second["selected_revision_id"])
        self.assertEqual(state["total"], 2)

    def test_review_is_invalidated_by_spoken_content_changes(self):
        plan = self.prepare()
        response = self.post(
            "/generation-plan/review",
            {
                "revision_id": plan["selected_revision_id"],
                "content_signature": plan["content_signature"],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertTrue(
            self.client.get(self.base + "/generation-plan/status").get_json()["items"][
                0
            ]["reviewed"]
        )
        with self.services.database.session() as session:
            segment = session.scalar(
                select(m.GenerationSegment).where(
                    m.GenerationSegment.plan_revision_id == plan["selected_revision_id"]
                )
            )
            segment.optimized_text = "Changed wording."
        self.assertFalse(
            self.client.get(self.base + "/generation-plan/status").get_json()["items"][
                0
            ]["reviewed"]
        )
        response = self.post(
            "/generation-plan/review",
            {
                "revision_id": plan["selected_revision_id"],
                "content_signature": plan["content_signature"],
            },
        )
        self.assertEqual(response.status_code, 409)

    def test_generation_freezes_prepared_words_and_rejects_a_later_edit(self):
        plan = self.prepare()
        run = self.services.generation.start(
            self.sid, speech_plan_revision_id=plan["selected_revision_id"]
        )
        with self.services.database.session() as session:
            stored = session.get(m.GenerationRun, run["id"])
            self.assertTrue(stored.settings_snapshot_json["speech_plan_frozen"])
            self.assertFalse(
                stored.settings_snapshot_json["text"]["llm_tts_optimization"]
            )
            segment = session.scalar(
                select(m.GenerationSegment).where(
                    m.GenerationSegment.plan_revision_id == plan["selected_revision_id"]
                )
            )
            segment.text = "Changed after queueing."
        with patch(
            "pandrator.web.workflow_handlers.hydrate_tts_settings",
            side_effect=AssertionError("must stop before inference"),
        ):
            with self.assertRaisesRegex(
                ValueError, "changed after generation was queued"
            ):
                self.services.workflow_handlers.run_generation(
                    {"generation_run_id": run["id"]}, lambda *_: None, threading.Event()
                )

    def test_editing_reviewed_plan_creates_copy_without_changing_approved_text(self):
        first = self.prepare()
        self.post(
            "/generation-plan/review",
            {
                "revision_id": first["selected_revision_id"],
                "content_signature": first["content_signature"],
            },
        )
        segment = self.services.generation.list_segments(self.sid)["items"][0]
        result = self.services.generation.update_segment(
            segment["id"], segment["revision"], {"text": "Editorial revision."}
        )
        self.assertNotEqual(result["id"], segment["id"])
        with self.services.database.session() as session:
            self.assertEqual(
                session.get(m.GenerationSegment, segment["id"]).text, segment["text"]
            )
            self.assertIsNotNone(
                session.get(m.SpeechPlanReview, first["selected_revision_id"])
            )
        state = self.client.get(self.base + "/generation-plan/status").get_json()
        self.assertNotEqual(
            state["selected_revision_id"], first["selected_revision_id"]
        )
        self.assertFalse(state["items"][0]["reviewed"])

    def test_batch_edits_copy_reviewed_plan_only_once(self):
        first = self.prepare()
        self.post(
            "/generation-plan/review",
            {
                "revision_id": first["selected_revision_id"],
                "content_signature": first["content_signature"],
            },
        )
        segments = self.services.generation.list_segments(self.sid)["items"]
        changes = [
            {
                "id": row["id"],
                "revision": row["revision"],
                "changes": {"text": row["text"] + " Edited."},
            }
            for row in segments
        ]
        result = self.services.generation.update_segments(self.sid, changes)
        self.assertEqual(len(result["items"]), len(changes))
        state = self.client.get(self.base + "/generation-plan/status").get_json()
        self.assertEqual(state["total"], 2)
        with self.services.database.session() as session:
            self.assertEqual(
                session.get(m.GenerationSegment, segments[0]["id"]).text,
                segments[0]["text"],
            )

    def test_replacement_timing_requires_explicit_confirmation(self):
        from pandrator.web.source_management import require_recording_timing_review

        first = self.upload(self.wav_data(100), "first.wav")
        second = self.upload(self.wav_data(200), "second.wav")
        self.assertEqual(
            self.change(first["source_asset_id"], "media").status_code, 200
        )
        response = self.change(second["source_asset_id"], "media")
        self.assertEqual(response.status_code, 200, response.get_json())
        state = response.get_json()
        self.assertTrue(state["timing_review"]["required"])
        with self.assertRaisesRegex(ValueError, "Verify speech timing"):
            require_recording_timing_review(
                self.services.database,
                self.sid,
                {"export_mode": "audio", "audio_mode": "mixed"},
            )
        confirmation = self.post(
            "/sources/confirm-timing",
            {
                "expected_revision": state["session_revision"],
                "media_artifact_id": second["artifact_id"],
            },
        )
        self.assertEqual(confirmation.status_code, 200, confirmation.get_json())
        require_recording_timing_review(
            self.services.database,
            self.sid,
            {"export_mode": "audio", "audio_mode": "mixed"},
        )

    def test_invalid_recording_does_not_reset_the_session(self):
        first = self.prepare()
        source = self.upload(b"not real media", "broken.mp4")
        response = self.change(source["source_asset_id"])
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertEqual(
            self.client.get(self.base + "/generation-plan/status").get_json()[
                "selected_revision_id"
            ],
            first["selected_revision_id"],
        )

    def test_new_session_alternative_preserves_all_original_work(self):
        first = self.prepare()
        replacement = self.upload(
            SRT.replace("Hello", "Goodbye").encode(), "new-session.srt"
        )
        preview = self.post(
            "/sources/change-preview",
            {"role": "primary", "new_source_asset_id": replacement["source_asset_id"]},
        ).get_json()
        response = self.post(
            "/sources/start-new-session",
            {
                "role": "primary",
                "new_source_asset_id": replacement["source_asset_id"],
                "expected_revision": preview["session_revision"],
                "impact_token": preview["impact_token"],
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        created_id = response.get_json()["session_id"]
        self.assertNotEqual(created_id, self.sid)
        self.assertEqual(
            self.client.get(self.base + "/generation-plan/status").get_json()[
                "selected_revision_id"
            ],
            first["selected_revision_id"],
        )
        self.assertEqual(
            self.client.get(self.base + "/sources/status").get_json()["primary"][
                "artifact_id"
            ],
            self.uploaded["artifact_id"],
        )
        new_state = self.client.get(
            f"/api/v1/sessions/{created_id}/sources/status"
        ).get_json()
        self.assertEqual(
            new_state["primary"]["artifact_id"], replacement["artifact_id"]
        )
        self.assertFalse(new_state["subtitle"]["adoption_required"])
        with self.services.database.session() as session:
            self.assertEqual(
                session.get(m.OutcomePlan, created_id).value_json,
                session.get(m.OutcomePlan, self.sid).value_json,
            )

    def test_file_bundle_after_reset_skips_tombstones_and_includes_attached_source(
        self,
    ):
        import zipfile
        import json
        from pathlib import Path
        from pandrator.web.bundles import SessionBundleService

        replacement = self.upload(
            SRT.replace("Hello", "Goodbye").encode(), "replacement.srt"
        )
        self.assertEqual(self.change(replacement["source_asset_id"]).status_code, 200)
        destination = Path(self.temp.name) / "after-reset.pandrator-session"
        SessionBundleService(self.services.database, self.services.paths).export_bundle(
            self.sid, destination
        )
        with zipfile.ZipFile(destination) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertIn(
            replacement["artifact_id"],
            [row["source_id"] for row in manifest["artifacts"]],
        )
        self.assertFalse(
            any(row["state"] == "deleted" for row in manifest["artifacts"])
        )

    def test_source_reset_retries_are_idempotent(self):
        preview = self.post("/sources/change-preview", {"role": "primary"}).get_json()
        first = self.change(None, preview=preview, key="same-reset-request")
        second = self.change(None, preview=preview, key="same-reset-request")
        self.assertEqual(first.status_code, 200, first.get_json())
        self.assertEqual(second.status_code, 200, second.get_json())
        self.assertEqual(first.get_json(), second.get_json())
        self.assertEqual(second.headers.get("Idempotency-Replayed"), "true")
