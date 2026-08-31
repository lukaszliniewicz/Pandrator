import json
import tempfile
import unittest

from sqlalchemy import select

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import ArtifactEdge, Document, Segment
from pandrator.web.schemas import (
    SpeechOptimizationDispatchBatchClaimResponse,
    SpeechOptimizationDispatchBatchSubmitResponse,
)
from tests.web_test_support import prepare_web_test_data_root


class SpeechOptimizationDispatchWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        token = bootstrap.issue()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.extension = self.app.extensions["pandrator"]

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def _headers(self, key: str | None = None):
        headers = {"X-CSRF-Token": self.csrf}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def _create_source(
        self,
        *,
        workflow_kind: str,
        role: str,
        filename: str,
        content: str,
    ):
        record = self.extension["sessions"].create(
            "Passive speech optimisation",
            workflow_kind=workflow_kind,
            source_language="en",
            target_language="de" if workflow_kind == "voiceover" else None,
        )
        directory = self.extension["paths"].sessions / record.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(content, encoding="utf-8")
        artifact = self.extension["artifacts"].register(
            path,
            kind=path.suffix.removeprefix("."),
            role=role,
            session_id=record.id,
        )
        return record, artifact, path

    def _create_run(self, session_id: str, **overrides):
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/speech-optimization-dispatch-runs",
            json={"instructions": "Prefer natural spoken forms.", **overrides},
            headers=self._headers("speech-create-1"),
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _claim(self, run_id: str, ordinal: int):
        response = self.client.post(
            f"/api/v1/speech-optimization-dispatch-runs/{run_id}/claim",
            json={"lease_seconds": 900},
            headers=self._headers(f"speech-claim-{ordinal}"),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        claimed = response.get_json()
        SpeechOptimizationDispatchBatchClaimResponse.model_validate(claimed)
        return claimed

    def _submit(self, claimed, items, ordinal: int):
        response = self.client.post(
            f"/api/v1/speech-optimization-dispatch-batches/{claimed['batch_id']}/submit",
            json={
                "lease_token": claimed["lease_token"],
                "result": {"kind": "speech_optimization", "items": items},
            },
            headers=self._headers(f"speech-submit-{ordinal}"),
        )
        self.assertIn(response.status_code, {200, 202}, response.get_json())
        payload = response.get_json()
        SpeechOptimizationDispatchBatchSubmitResponse.model_validate(payload)
        return payload

    def test_json_batches_are_sequential_and_materialize_normal_artifact(self):
        rows = [
            {"processed_sentence": "Dr. Jones arrived.", "language": "en"},
            {"processed_sentence": "Room 101 is ready.", "language": "en"},
            {"processed_sentence": "Chapter IV begins.", "language": "en"},
        ]
        record, source, _path = self._create_source(
            workflow_kind="audiobook",
            role="prepared_text",
            filename="prepared.json",
            content=json.dumps(rows),
        )
        run = self._create_run(
            record.id,
            max_units_per_batch=2,
            context_before=1,
            context_after=1,
        )
        self.assertEqual(2, run["batch_count"])
        self.assertIsNone(run.get("provider"))

        first = self._claim(run["id"], 1)
        self.assertEqual([1, 2], first["batch"]["valid_unit_ids"])
        self.assertEqual([], first["batch"]["context"]["previous_output"])
        self.assertEqual(
            "Chapter IV begins.",
            first["batch"]["context"]["following_source"][0]["text"],
        )

        rejected = self.client.post(
            f"/api/v1/speech-optimization-dispatch-batches/{first['batch_id']}/submit",
            json={
                "lease_token": first["lease_token"],
                "result": {
                    "kind": "speech_optimization",
                    "items": [
                        {"unit_id": 2, "text": "Room one oh one is ready."},
                        {"unit_id": 1, "text": "Doctor Jones arrived."},
                    ],
                },
            },
            headers=self._headers("speech-submit-rejected"),
        )
        self.assertEqual(422, rejected.status_code, rejected.get_json())
        self.assertEqual("invalid_model_response", rejected.get_json()["error"]["code"])

        self._submit(
            first,
            [
                {"unit_id": 1, "text": "Doctor Jones arrived."},
                {"unit_id": 2, "text": "Room one oh one is ready."},
            ],
            1,
        )
        replay = self.client.post(
            f"/api/v1/speech-optimization-dispatch-runs/{run['id']}/claim",
            json={"lease_seconds": 900},
            headers=self._headers("speech-claim-1"),
        )
        self.assertEqual(200, replay.status_code, replay.get_json())
        self.assertEqual(first["batch_id"], replay.get_json()["batch_id"])
        self.assertEqual("completed", replay.get_json()["batch_status"])

        second = self._claim(run["id"], 2)
        self.assertEqual([3], second["batch"]["valid_unit_ids"])
        self.assertEqual(
            "Room one oh one is ready.",
            second["batch"]["context"]["previous_output"][0]["text"],
        )
        final = self._submit(
            second,
            [{"unit_id": 3, "text": "Chapter Four begins."}],
            2,
        )
        self.assertTrue(final["finalized"])

        artifact, output_path = self.extension["artifacts"].resolve(
            final["final_artifact_id"]
        )
        self.assertEqual("tts_optimized", artifact.role)
        self.assertIsNone(artifact.metadata_json["provider"])
        self.assertIsNone(artifact.metadata_json["model"])
        output = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [
                "Doctor Jones arrived.",
                "Room one oh one is ready.",
                "Chapter Four begins.",
            ],
            [item["tts_optimized_sentence"] for item in output],
        )
        self.assertEqual(
            [item["processed_sentence"] for item in rows],
            [item["source_text"] for item in output],
        )
        with self.extension["database"].session() as session:
            self.assertIsNotNone(
                session.get(ArtifactEdge, (source.id, final["final_artifact_id"]))
            )

    def test_srt_timing_is_actionable_once_and_revision_preserves_speakers(self):
        srt = (
            "1\n00:00:00,000 --> 00:00:01,200\n"
            "[SPEAKER_00] Dr. Jones arrived.\n\n"
            "2\n00:00:01,300 --> 00:00:02,500\n"
            "[SPEAKER_01] Room 101 is ready.\n"
        )
        record, source, _path = self._create_source(
            workflow_kind="voiceover",
            role="translation",
            filename="translation.srt",
            content=srt,
        )
        run = self._create_run(
            record.id,
            source_artifact_id=source.id,
            language="en",
            voice_language="en-US",
        )
        claimed = self._claim(run["id"], 1)
        self.assertEqual(
            {"start_ms": 0, "end_ms": 1200, "duration_ms": 1200},
            claimed["batch"]["units"][0]["timing"],
        )
        self.assertNotIn(
            "timing", claimed["batch"]["context"].get("previous_output", {})
        )
        final = self._submit(
            claimed,
            [
                {"unit_id": 1, "text": "Doctor Jones arrived."},
                {"unit_id": 2, "text": "Room one oh one is ready."},
            ],
            1,
        )
        artifact, output_path = self.extension["artifacts"].resolve(
            final["final_artifact_id"]
        )
        output = parse_srt(output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(0, 1200), (1300, 2500)], [(item.start_ms, item.end_ms) for item in output]
        )
        self.assertEqual(
            ["Doctor Jones arrived.", "Room one oh one is ready."],
            [item.text for item in output],
        )
        self.assertEqual(source.id, artifact.metadata_json["source_artifact_id"])
        with self.extension["database"].session() as session:
            document = session.scalar(
                select(Document).where(
                    Document.session_id == record.id,
                    Document.stage == "tts_optimization",
                )
            )
            self.assertIsNotNone(document)
            segments = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == final["result_revision_id"])
                    .order_by(Segment.ordinal)
                ).all()
            )
            self.assertEqual(
                ["SPEAKER_00", "SPEAKER_01"], [item.speaker for item in segments]
            )

    def test_attached_library_source_is_eligible_and_remains_pinned(self):
        record = self.extension["sessions"].create(
            "Attached passive speech source",
            workflow_kind="voiceover",
            source_language="de",
            target_language="de",
        )
        source_path = self.extension["paths"].uploads / "attached-course.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,500\nIARF heißt Sie willkommen.\n",
            encoding="utf-8",
        )
        source = self.extension["artifacts"].register(
            source_path,
            kind="srt",
            role="upload",
            session_id=None,
        )
        asset = self.extension["source_library"].ensure_for_artifact(
            source.id,
            display_name="attached-course.srt",
            kind="srt",
        )
        self.extension["source_library"].attach(record.id, asset.id)

        run = self._create_run(record.id)
        self.assertEqual(source.id, run["source_artifact_id"])
        claimed = self._claim(run["id"], 1)
        final = self._submit(
            claimed,
            [{"unit_id": 1, "text": "I A R F heißt Sie willkommen."}],
            1,
        )
        artifact, output_path = self.extension["artifacts"].resolve(
            final["final_artifact_id"]
        )
        self.assertEqual(record.id, artifact.session_id)
        self.assertIn("I A R F", output_path.read_text(encoding="utf-8"))

    def test_audiobook_auto_selection_ignores_original_epub(self):
        record, _upload, _path = self._create_source(
            workflow_kind="audiobook",
            role="upload",
            filename="book.epub",
            content="not an extracted text source",
        )
        response = self.client.post(
            f"/api/v1/sessions/{record.id}/speech-optimization-dispatch-runs",
            json={},
            headers=self._headers("speech-no-materialized-source"),
        )
        self.assertEqual(422, response.status_code, response.get_json())
        self.assertEqual("source_not_found", response.get_json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
