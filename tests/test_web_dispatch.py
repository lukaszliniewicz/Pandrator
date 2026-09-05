import json
import tempfile
import unittest
from datetime import timedelta

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.artifact_selection import choose_artifact
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    DispatchBatch,
    DispatchRun,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionStageSelection,
    SubtitleEvidence,
    utcnow,
)
from pandrator.web.schemas import (
    DispatchBatchClaimResponse,
    DispatchBatchSubmitResponse,
)
from tests.web_test_support import prepare_web_test_data_root


class DispatchWebTests(unittest.TestCase):
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

    def _source(
        self,
        *,
        target_language: str | None = None,
        texts: tuple[str, ...] = ("Hello world.", "Goodbye."),
    ):
        session = self.extension["sessions"].create(
            "Dispatch",
            workflow_kind="subtitles",
            source_language="en",
            target_language=target_language,
        )
        directory = self.extension["paths"].sessions / session.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        source_path = directory / "source.srt"
        source_path.write_text(
            "\n".join(
                f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\n{text}\n"
                for index, text in enumerate(texts, start=1)
            ),
            encoding="utf-8",
        )
        artifact = self.extension["artifacts"].register(
            source_path,
            kind="srt",
            role="transcription",
            session_id=session.id,
        )
        self.extension["workflow_handlers"]._store_srt_document(
            session.id,
            artifact,
            "transcription",
            language="en",
        )
        return session.id, artifact.id

    def _stage_artifact(
        self,
        session_id: str,
        *,
        name: str,
        role: str,
        text: str,
        parent_id: str,
        language: str = "en",
    ) -> str:
        directory = (
            self.extension["paths"].sessions
            / self.extension["sessions"].get(session_id).storage_key
        )
        path = directory / name
        path.write_text(
            f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n",
            encoding="utf-8",
        )
        artifact = self.extension["artifacts"].register(
            path,
            kind="srt",
            role=role,
            session_id=session_id,
            parent_ids=[parent_id],
        )
        self.extension["workflow_handlers"]._store_srt_document(
            session_id,
            artifact,
            role,
            language=language,
        )
        return artifact.id

    def _headers(self, key: str | None = None):
        headers = {"X-CSRF-Token": self.csrf}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def _create(self, session_id, **overrides):
        body = {"kind": "correction", **overrides}
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/dispatch-runs",
            json=body,
            headers=self._headers(),
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _claim(self, run_id, key="claim-key-123"):
        response = self.client.post(
            f"/api/v1/dispatch-runs/{run_id}/claim",
            json={},
            headers=self._headers(key),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def _submit_correction(self, claim, key, *, suffix="!", context_delta=None):
        cue = claim["batch"]["cues"][0]
        body = {
            "lease_token": claim["lease_token"],
            "result": {
                "kind": "correction",
                "operations": [
                    {
                        "action": "edit",
                        "cue_ids": [cue["cue_id"]],
                        "texts": [cue["text"] + suffix],
                    }
                ],
            },
        }
        if context_delta is not None:
            body["context_delta"] = context_delta
        return self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json=body,
            headers=self._headers(key),
        )

    def test_correction_end_to_end_and_redacted_metadata(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        self.assertEqual("correction", run["output_role"])
        claim = self._claim(run["id"])
        DispatchBatchClaimResponse.model_validate(claim)
        self.assertNotIn("source_text", claim)
        self.assertNotIn("prompt", claim)
        self.assertEqual(1, claim["batch_ordinal"])
        self.assertEqual("correction", claim["task"]["kind"])
        self.assertEqual("correction", claim["task"]["output_role"])
        self.assertEqual(
            ["Hello world.", "Goodbye."],
            [cue["text"] for cue in claim["batch"]["cues"]],
        )
        self.assertEqual(
            [1, 2],
            claim["batch"]["valid_cue_ids"],
        )
        submit = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "kind": "correction",
                    "operations": [
                        {"action": "edit", "cue_ids": [1], "texts": ["Hello!"]},
                        {"action": "edit", "cue_ids": [2], "texts": ["Bye!"]},
                    ],
                },
            },
            headers=self._headers("submit-key-123"),
        )
        self.assertEqual(200, submit.status_code, submit.get_json())
        DispatchBatchSubmitResponse.model_validate(submit.get_json())
        self.assertEqual("completed", submit.get_json()["run_status"])
        fetched = self.client.get(f"/api/v1/dispatch-runs/{run['id']}")
        self.assertEqual(200, fetched.status_code)
        serialized = fetched.get_data(as_text=True)
        self.assertNotIn("input_json", serialized)
        self.assertNotIn("normalized_output_json", serialized)
        self.assertNotIn("lease_token", serialized)

    def test_correction_can_escalate_and_materialize_a_reviewable_uncertainty(self):
        session_id, source_id = self._source(
            texts=("Culture. Yes.", "We elected two co-chairs."),
        )
        provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "gemini", "label": "Configured Gemini"},
            headers=self._headers(),
        ).get_json()
        audio_model = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={
                "model_id": "gemini-audio-checker",
                "is_active": True,
                "input_modalities": ["text", "audio"],
            },
            headers=self._headers(),
        ).get_json()
        with self.extension["database"].session() as session:
            source = session.get(Artifact, source_id)
            revision_id = str(source.metadata_json["revision_id"])
            segment = session.scalar(
                select(Segment).where(
                    Segment.revision_id == revision_id,
                    Segment.ordinal == 0,
                )
            )
            session.add(
                SubtitleEvidence(
                    id="evidence-request-1",
                    session_id=session_id,
                    source_artifact_id=source_id,
                    source_revision_id=revision_id,
                    source_segment_id=segment.id,
                    cue_id=1,
                    start_ms=int(segment.start_ms),
                    end_ms=int(segment.end_ms),
                    clip_start_ms=0,
                    clip_end_ms=int(segment.end_ms) + 2_000,
                    reason="Confirm the title.",
                    routes_json=["whisper", "moss"],
                    audio_model_ids_json=[],
                    status="completed",
                    candidates_json=[],
                    resolution_json={},
                )
            )
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"], "quality-claim-key")
        self.assertEqual(session_id, claim["task"]["session_id"])
        self.assertEqual(source_id, claim["task"]["source_artifact_id"])
        self.assertTrue(claim["task"]["quality_policy"]["deletion_allowed"])
        self.assertEqual(
            "pandrator_request_subtitle_evidence",
            claim["task"]["quality_policy"]["evidence_tool"],
        )
        self.assertIn(
            "audio_llm", claim["task"]["quality_policy"]["available_routes"]
        )
        self.assertEqual(
            [
                {
                    "id": audio_model["id"],
                    "model_id": "gemini-audio-checker",
                    "provider": "Configured Gemini",
                    "input_modality_status": "declared",
                    "timing_kind": "bounded_clip",
                }
            ],
            claim["task"]["quality_policy"]["audio_witness_models"],
        )
        self.assertIn(
            "do not use it as a timing authority",
            claim["task"]["quality_policy"]["audio_witness_policy"],
        )
        self.assertIn("Deletion is an ordinary editorial action", claim["task"]["instructions"])

        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "kind": "correction",
                    "operations": [],
                    "uncertainties": [
                        {
                            "cue_id": 1,
                            "reason": "Whisper and MOSS disagree on the title.",
                            "evidence_ids": ["evidence-request-1"],
                        }
                    ],
                },
            },
            headers=self._headers("quality-submit-key"),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        result_artifact_id = response.get_json()["result_artifact_id"]
        with self.extension["database"].session() as session:
            artifact = session.get(Artifact, result_artifact_id)
            revision = session.get(
                DocumentRevision, artifact.metadata_json["revision_id"]
            )
            segments = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == revision.id)
                    .order_by(Segment.ordinal)
                ).all()
            )
        self.assertEqual(1, artifact.metadata_json["uncertain_segment_count"])
        self.assertEqual("uncertain", segments[0].metadata_json["review_state"])
        self.assertEqual(
            ["evidence-request-1"], segments[0].metadata_json["evidence_ids"]
        )
        self.assertEqual("clear", segments[1].metadata_json["review_state"])

        second_run = self._create(session_id, source_artifact_id=source_id)
        second_claim = self._claim(second_run["id"], "delete-uncertain-claim")
        invalid = self.client.post(
            f"/api/v1/dispatch-batches/{second_claim['batch_id']}/submit",
            json={
                "lease_token": second_claim["lease_token"],
                "result": {
                    "kind": "correction",
                    "operations": [
                        {"action": "delete", "cue_ids": [1], "texts": []}
                    ],
                    "uncertainties": [
                        {"cue_id": 1, "reason": "Cannot be both states."}
                    ],
                },
            },
            headers=self._headers("delete-uncertain-submit"),
        )
        self.assertEqual(422, invalid.status_code, invalid.get_json())

    def test_correction_of_translation_appends_a_translation_revision(self):
        session_id, transcription_id = self._source(target_language="de")
        translation_id = self._stage_artifact(
            session_id,
            name="translation-de.srt",
            role="translation",
            text="Das wichtig ist,",
            parent_id=transcription_id,
            language="de",
        )
        with self.extension["database"].session() as session:
            source_artifact = session.get(Artifact, translation_id)
            source_revision_id = source_artifact.metadata_json["revision_id"]
            source_revision = session.get(DocumentRevision, source_revision_id)
            source_document_id = source_revision.document_id
            source_revision_number = source_revision.revision_number

        run = self._create(
            session_id,
            source_artifact_id=translation_id,
            no_remove_subtitles=True,
        )
        self.assertEqual("correction", run["kind"])
        self.assertEqual("translation", run["output_role"])
        self.assertEqual("de", run["source_language"])
        self.assertIsNone(run["target_language"])

        claim = self._claim(run["id"], "translation-correction-claim")
        DispatchBatchClaimResponse.model_validate(claim)
        self.assertEqual("correction", claim["task"]["kind"])
        self.assertEqual("translation", claim["task"]["output_role"])
        self.assertEqual("de", claim["task"]["source_language"])
        self.assertIsNone(claim["task"]["target_language"])
        self.assertEqual("Das wichtig ist,", claim["batch"]["cues"][0]["text"])

        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "kind": "correction",
                    "operations": [
                        {
                            "action": "edit",
                            "cue_ids": [1],
                            "texts": ["Das ist wichtig."],
                        }
                    ],
                },
            },
            headers=self._headers("translation-correction-submit"),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["finalized"])

        with self.extension["database"].session() as session:
            result_artifact = session.get(Artifact, payload["result_artifact_id"])
            self.assertEqual("translation", result_artifact.role)
            self.assertEqual("current", result_artifact.state)
            self.assertEqual("stale", session.get(Artifact, translation_id).state)
            self.assertIsNotNone(
                session.get(ArtifactEdge, (translation_id, result_artifact.id))
            )
            self.assertEqual("translation", result_artifact.metadata_json["stage"])
            self.assertEqual(
                "correction", result_artifact.metadata_json["dispatch_kind"]
            )
            self.assertEqual("de", result_artifact.metadata_json["language"])

            result_revision = session.get(
                DocumentRevision, payload["result_revision_id"]
            )
            self.assertEqual(source_document_id, result_revision.document_id)
            self.assertEqual(source_revision_id, result_revision.parent_revision_id)
            self.assertEqual(
                source_revision_number + 1, result_revision.revision_number
            )
            document = session.get(Document, result_revision.document_id)
            self.assertEqual("translation", document.stage)
            self.assertEqual("de", document.language)
            self.assertEqual(result_revision.id, document.active_revision_id)

            result_segment = session.scalar(
                select(Segment).where(Segment.revision_id == result_revision.id)
            )
            self.assertEqual("Das ist wichtig.", result_segment.text)
            self.assertEqual(
                (0, 1000), (result_segment.start_ms, result_segment.end_ms)
            )
            self.assertIsNotNone(
                session.scalar(
                    select(SegmentLineage).where(
                        SegmentLineage.child_segment_id == result_segment.id
                    )
                )
            )
            translation_selection = session.get(
                SessionStageSelection, (session_id, "translate")
            )
            self.assertEqual(result_artifact.id, translation_selection.artifact_id)
            self.assertIsNone(
                session.get(SessionStageSelection, (session_id, "correct"))
            )

    def test_correction_of_translation_rejects_language_relabeling(self):
        session_id, transcription_id = self._source(target_language="de")
        translation_id = self._stage_artifact(
            session_id,
            name="translation-language.srt",
            role="translation",
            text="Deutsch.",
            parent_id=transcription_id,
            language="de",
        )
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/dispatch-runs",
            json={
                "kind": "correction",
                "source_artifact_id": translation_id,
                "source_language": "en",
            },
            headers=self._headers(),
        )
        self.assertEqual(422, response.status_code, response.get_json())
        self.assertEqual(
            "source_language_mismatch", response.get_json()["error"]["code"]
        )

    def test_correction_of_translation_is_fenced_by_translation_selection(self):
        session_id, transcription_id = self._source(target_language="de")
        first_translation_id = self._stage_artifact(
            session_id,
            name="translation-first.srt",
            role="translation",
            text="Erste Fassung.",
            parent_id=transcription_id,
            language="de",
        )
        second_translation_id = self._stage_artifact(
            session_id,
            name="translation-second.srt",
            role="translation",
            text="Zweite Fassung.",
            parent_id=transcription_id,
            language="de",
        )
        with self.extension["database"].session() as session:
            choose_artifact(session, session_id, "translate", first_translation_id)
        run = self._create(
            session_id,
            source_artifact_id=first_translation_id,
        )
        claim = self._claim(run["id"], "translated-selection-claim")
        with self.extension["database"].session() as session:
            choose_artifact(session, session_id, "translate", second_translation_id)
        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {"kind": "correction", "operations": []},
            },
            headers=self._headers("translated-selection-submit"),
        )
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("finalization_conflict", response.get_json()["error"]["code"])
        self.assertEqual(
            ["translate"],
            response.get_json()["error"]["details"]["changed_stage_keys"],
        )

    def test_translation_and_strict_sequential_claiming(self):
        session_id, source_id = self._source(target_language="pl")
        run = self._create(
            session_id,
            kind="translation",
            source_artifact_id=source_id,
            char_limit=1,
            glossary={"Nautilus": "Nautylus"},
        )
        self.assertGreater(run["batch_count"], 1)
        first = self._claim(run["id"], "first-claim-key")
        self.assertEqual(
            {"Nautilus": "Nautylus"},
            first["task"]["glossary"],
        )
        self.assertNotIn("Nautylus", first["task"]["instructions"])
        self.assertIn("glossary_updates", first["task"]["instructions"])
        busy = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("second-claim-key"),
        )
        self.assertEqual(409, busy.status_code)
        self.assertTrue(busy.get_json()["error"]["details"]["retryable"])
        result = self.client.post(
            f"/api/v1/dispatch-batches/{first['batch_id']}/submit",
            json={
                "lease_token": first["lease_token"],
                "result": {
                    "kind": "translation",
                    "translations": [
                        {
                            "cue_id": first["batch"]["cues"][0]["cue_id"],
                            "text": "Cześć.",
                        }
                    ],
                    "glossary_updates": {
                        "nautilus": "MODEL_OVERRIDE",
                        "Captain Nemo": "Kapitan Nemo",
                    },
                },
            },
            headers=self._headers("first-submit-key"),
        )
        self.assertEqual(200, result.status_code, result.get_json())
        accepted_claim_replay = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("first-claim-key"),
        )
        self.assertEqual(200, accepted_claim_replay.status_code)
        self.assertEqual("completed", accepted_claim_replay.get_json()["batch_status"])
        self.assertEqual("running", accepted_claim_replay.get_json()["run_status"])
        second = self._claim(run["id"], "second-claim-key")
        self.assertNotEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(
            {
                "Nautilus": "Nautylus",
                "Captain Nemo": "Kapitan Nemo",
            },
            second["task"]["glossary"],
        )

    def test_translation_claim_separates_actionable_cues_from_boundary_context(self):
        session_id, source_id = self._source(
            target_language="pl",
            texts=("ALPHA_UNIQUE", "BETA_UNIQUE", "GAMMA_UNIQUE"),
        )
        run = self._create(
            session_id,
            kind="translation",
            source_artifact_id=source_id,
            char_limit=1,
        )
        first = self._claim(run["id"], "local-first-claim")
        self.assertEqual(
            ["ALPHA_UNIQUE"],
            [cue["text"] for cue in first["batch"]["cues"]],
        )
        self.assertEqual([], first["batch"]["context"]["previous_output"])
        self.assertEqual(
            ["BETA_UNIQUE"],
            [cue["text"] for cue in first["batch"]["context"]["following_source"]],
        )
        submitted = self.client.post(
            f"/api/v1/dispatch-batches/{first['batch_id']}/submit",
            json={
                "lease_token": first["lease_token"],
                "result": {
                    "kind": "translation",
                    "translations": [
                        {"cue_id": 1, "text": "PIERWSZY_UNIQUE"},
                    ],
                },
            },
            headers=self._headers("local-first-submit"),
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())
        second = self._claim(run["id"], "local-second-claim")
        self.assertEqual(
            ["BETA_UNIQUE"],
            [cue["text"] for cue in second["batch"]["cues"]],
        )
        self.assertEqual(
            ["PIERWSZY_UNIQUE"],
            [cue["text"] for cue in second["batch"]["context"]["previous_output"]],
        )
        self.assertEqual(
            ["GAMMA_UNIQUE"],
            [cue["text"] for cue in second["batch"]["context"]["following_source"]],
        )
        self.assertNotIn("timing", json.dumps(second["batch"]["context"]))

    def test_parallel_waves_share_capsule_and_gate_next_wave(self):
        session_id, source_id = self._source(
            texts=("A", "B", "C", "D", "E"),
        )
        run = self._create(
            session_id,
            source_artifact_id=source_id,
            char_limit=1,
            execution_mode="parallel",
            max_parallel_batches=3,
            context_capsule={"overview": "shared"},
        )
        claims = [
            self._claim(run["id"], f"parallel-claim-{ordinal:02d}")
            for ordinal in range(3)
        ]
        self.assertEqual([1, 2, 3], [claim["batch_ordinal"] for claim in claims])
        self.assertEqual(
            [
                {
                    "execution_mode": "parallel",
                    "max_parallel_batches": 3,
                    "wave_number": 1,
                    "wave_batch_count": 3,
                    "context_capsule": {
                        "overview": "shared",
                        "terminology": {},
                        "entities": {},
                        "style_rules": [],
                        "decisions": [],
                        "notes": [],
                    },
                }
            ]
            * 3,
            [claim["delegation"] for claim in claims],
        )
        self.assertEqual([], claims[0]["batch"]["context"]["previous_source"])
        self.assertEqual(
            ["A"],
            [cue["text"] for cue in claims[1]["batch"]["context"]["previous_source"]],
        )
        busy = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("parallel-claim-03"),
        )
        self.assertEqual(409, busy.status_code)
        self.assertEqual("dispatch_busy", busy.get_json()["error"]["code"])
        self.assertTrue(busy.get_json()["error"]["details"]["retryable"])

        self.assertEqual(
            200,
            self._submit_correction(claims[2], "parallel-submit-02").status_code,
        )
        self.assertEqual(
            200,
            self._submit_correction(claims[0], "parallel-submit-00").status_code,
        )
        still_busy = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("parallel-claim-04"),
        )
        self.assertEqual(409, still_busy.status_code)
        self.assertEqual("dispatch_busy", still_busy.get_json()["error"]["code"])
        self.assertEqual(
            200,
            self._submit_correction(claims[1], "parallel-submit-01").status_code,
        )
        next_claim = self._claim(run["id"], "parallel-claim-05")
        self.assertEqual(4, next_claim["batch_ordinal"])
        self.assertEqual(2, next_claim["delegation"]["wave_number"])
        self.assertEqual(
            ["C!"],
            [cue["text"] for cue in next_claim["batch"]["context"]["previous_output"]],
        )

    def test_parallel_reclaim_does_not_disturb_active_sibling(self):
        session_id, source_id = self._source(
            texts=("A", "B", "C"),
        )
        run = self._create(
            session_id,
            source_artifact_id=source_id,
            char_limit=1,
            execution_mode="parallel",
            max_parallel_batches=3,
        )
        first = self._claim(run["id"], "reclaim-claim-00")
        sibling = self._claim(run["id"], "reclaim-claim-01")
        with self.extension["database"].session() as session:
            expired = session.get(DispatchBatch, first["batch_id"])
            active = session.get(DispatchBatch, sibling["batch_id"])
            expired.lease_expires_at = utcnow() - timedelta(seconds=1)
            active_token = active.lease_token
            active_expiry = active.lease_expires_at
        reclaimed = self._claim(run["id"], "reclaim-claim-02")
        self.assertEqual(first["batch_id"], reclaimed["batch_id"])
        self.assertNotEqual(first["lease_token"], reclaimed["lease_token"])
        with self.extension["database"].session() as session:
            active = session.get(DispatchBatch, sibling["batch_id"])
            self.assertEqual("leased", active.status)
            self.assertEqual(active_token, active.lease_token)
            self.assertEqual(active_expiry, active.lease_expires_at)

    def test_parallel_context_and_glossary_merge_by_ordinal(self):
        session_id, source_id = self._source(
            target_language="pl",
            texts=("A", "B", "C", "D"),
        )
        run = self._create(
            session_id,
            kind="translation",
            source_artifact_id=source_id,
            char_limit=1,
            execution_mode="parallel",
            max_parallel_batches=2,
            context_capsule={"overview": "base"},
        )
        first, second = (
            self._claim(run["id"], "ordinal-claim-00"),
            self._claim(run["id"], "ordinal-claim-01"),
        )

        def submit(claim, key, text, note, glossary):
            cue = claim["batch"]["cues"][0]
            return self.client.post(
                f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
                json={
                    "lease_token": claim["lease_token"],
                    "result": {
                        "kind": "translation",
                        "translations": [{"cue_id": cue["cue_id"], "text": text}],
                        "glossary_updates": glossary,
                    },
                    "context_delta": {"notes": [note]},
                },
                headers=self._headers(key),
            )

        self.assertEqual(
            200,
            submit(
                second, "ordinal-submit-01", "C!", "late", {"Term": "later"}
            ).status_code,
        )
        self.assertEqual(
            200,
            submit(
                first, "ordinal-submit-00", "A!", "early", {"Term": "earlier"}
            ).status_code,
        )
        next_claim = self._claim(run["id"], "ordinal-claim-02")
        self.assertEqual(
            ["early", "late"], next_claim["delegation"]["context_capsule"]["notes"]
        )
        self.assertEqual({"Term": "later"}, next_claim["task"]["glossary"])

    def test_context_delta_retry_hash_requires_same_delta(self):
        session_id, source_id = self._source(
            texts=("A", "B"),
        )
        run = self._create(session_id, source_artifact_id=source_id, char_limit=1)
        claim = self._claim(run["id"], "delta-hash-claim")
        first = self._submit_correction(
            claim,
            "delta-hash-submit",
            context_delta={"notes": ["learned"]},
        )
        replay = self._submit_correction(
            claim,
            "delta-hash-submit",
            context_delta={"notes": ["learned"]},
        )
        self.assertIn(first.status_code, {200, 202})
        self.assertIn(replay.status_code, {200, 202})
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        conflict = self._submit_correction(
            claim,
            "delta-hash-submit",
            context_delta={"notes": ["changed"]},
        )
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])

    def test_claim_timing_is_included_once_or_fully_excluded(self):
        session_id, source_id = self._source()
        full_run = self._create(session_id, source_artifact_id=source_id)
        full_claim = self._claim(full_run["id"], "full-timing-claim")
        self.assertEqual("full", full_claim["task"]["timing_context_mode"])
        self.assertIn(
            "`task.substantial_gap_ms`",
            full_claim["task"]["instructions"],
        )
        self.assertNotIn("2000 ms", full_claim["task"]["instructions"])
        self.assertEqual(2000, full_claim["task"]["substantial_gap_ms"])
        self.assertEqual(
            full_claim["batch"]["cue_count"],
            json.dumps(full_claim).count('"start_ms"'),
        )
        for cue in full_claim["batch"]["cues"]:
            self.assertNotIn("batch_ordinal", cue)
            self.assertEqual(
                {"start_ms", "end_ms"}, set(cue["timing"]) & {"start_ms", "end_ms"}
            )

        none_run = self._create(
            session_id,
            source_artifact_id=source_id,
            timing_context_mode="none",
        )
        none_claim = self._claim(none_run["id"], "no-timing-claim")
        self.assertEqual("none", none_claim["task"]["timing_context_mode"])
        self.assertIsNone(none_claim["task"]["substantial_gap_ms"])
        self.assertNotIn('"timing"', json.dumps(none_claim))
        self.assertNotIn('"start_ms"', json.dumps(none_claim))

    def test_legacy_false_timing_flag_maps_to_none(self):
        session_id, source_id = self._source()
        run = self._create(
            session_id,
            source_artifact_id=source_id,
            include_timing_context=False,
        )
        claim = self._claim(run["id"], "legacy-no-timing-claim")
        self.assertEqual("none", claim["task"]["timing_context_mode"])
        self.assertNotIn('"timing"', json.dumps(claim))

    def test_invalid_response_retains_lease_and_reclaim_fences_old_token(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"])
        invalid = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={"lease_token": claim["lease_token"], "response_text": "nope"},
            headers=self._headers("invalid-submit-key"),
        )
        self.assertEqual(422, invalid.status_code)
        self.assertEqual(
            "leased",
            self.client.get(f"/api/v1/dispatch-runs/{run['id']}").get_json()["batches"][
                0
            ]["status"],
        )
        with self.extension["database"].session() as session:
            batch = session.get(DispatchBatch, claim["batch_id"])
            batch.lease_expires_at = utcnow() - timedelta(seconds=1)
        reclaimed = self._claim(run["id"], "reclaim-key")
        self.assertNotEqual(claim["lease_token"], reclaimed["lease_token"])
        stale = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "response_text": '{"operations": []}',
            },
            headers=self._headers("stale-submit-key"),
        )
        self.assertEqual(409, stale.status_code)

    def test_expired_claim_replay_gets_a_fresh_lease(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"], "expiring-claim-key")
        with self.extension["database"].session() as session:
            batch = session.get(DispatchBatch, claim["batch_id"])
            batch.lease_expires_at = utcnow() - timedelta(seconds=1)
        replay = self._claim(run["id"], "expiring-claim-key")
        self.assertEqual(claim["batch_id"], replay["batch_id"])
        self.assertNotEqual(claim["lease_token"], replay["lease_token"])

    def test_rejects_a_run_that_removes_every_subtitle(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"])
        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "response_text": json.dumps(
                    {"operations": [{"action": "delete", "ids": [1, 2], "texts": []}]}
                ),
            },
            headers=self._headers("remove-all-submit-key"),
        )
        self.assertEqual(422, response.status_code, response.get_json())
        self.assertEqual("invalid_model_response", response.get_json()["error"]["code"])
        status = self.client.get(f"/api/v1/dispatch-runs/{run['id']}").get_json()
        self.assertEqual("leased", status["batches"][0]["status"])

    def test_claim_and_submit_replays_are_idempotent(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        first = self._claim(run["id"], "same-claim-key")
        replay = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("same-claim-key"),
        )
        self.assertEqual(200, replay.status_code)
        self.assertEqual(first["batch_id"], replay.get_json()["batch_id"])
        self.assertEqual(first["lease_token"], replay.get_json()["lease_token"])
        response_text = '{"operations": []}'
        submitted = self.client.post(
            f"/api/v1/dispatch-batches/{first['batch_id']}/submit",
            json={"lease_token": first["lease_token"], "response_text": response_text},
            headers=self._headers("same-submit-key"),
        )
        replay_submit = self.client.post(
            f"/api/v1/dispatch-batches/{first['batch_id']}/submit",
            json={"lease_token": first["lease_token"], "response_text": response_text},
            headers=self._headers("same-submit-key"),
        )
        self.assertEqual(200, submitted.status_code)
        self.assertEqual(200, replay_submit.status_code)
        self.assertEqual("true", replay_submit.headers["Idempotency-Replayed"])
        self.assertEqual(submitted.get_json(), replay_submit.get_json())

    def test_renew_and_release_replays_are_idempotent(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"])
        renew_url = f"/api/v1/dispatch-batches/{claim['batch_id']}/renew"
        renew_body = {
            "lease_token": claim["lease_token"],
            "lease_seconds": 600,
        }
        renewed = self.client.post(
            renew_url,
            json=renew_body,
            headers=self._headers("renew-replay-key"),
        )
        replayed_renewal = self.client.post(
            renew_url,
            json=renew_body,
            headers=self._headers("renew-replay-key"),
        )
        self.assertEqual(200, renewed.status_code, renewed.get_json())
        self.assertEqual(renewed.get_json(), replayed_renewal.get_json())
        self.assertEqual("true", replayed_renewal.headers["Idempotency-Replayed"])

        release_url = f"/api/v1/dispatch-batches/{claim['batch_id']}/release"
        release_body = {"lease_token": claim["lease_token"]}
        released = self.client.post(
            release_url,
            json=release_body,
            headers=self._headers("release-replay-key"),
        )
        replayed_release = self.client.post(
            release_url,
            json=release_body,
            headers=self._headers("release-replay-key"),
        )
        self.assertEqual(200, released.status_code, released.get_json())
        self.assertEqual(released.get_json(), replayed_release.get_json())
        self.assertEqual("true", replayed_release.headers["Idempotency-Replayed"])

    def test_finalization_conflict_is_reported(self):
        session_id, source_id = self._source()
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"])
        directory = (
            self.extension["paths"].sessions
            / self.extension["sessions"].get(session_id).storage_key
        )
        competitor_path = directory / "competitor.srt"
        competitor_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nCompetitor.\n", encoding="utf-8"
        )
        competitor = self.extension["artifacts"].register(
            competitor_path,
            kind="srt",
            role="correction",
            session_id=session_id,
            parent_ids=[source_id],
        )
        self.extension["workflow_handlers"]._store_srt_document(
            session_id, competitor, "correction", language="en"
        )
        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "response_text": '{"operations": []}',
            },
            headers=self._headers("conflict-submit-key"),
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual("finalization_conflict", response.get_json()["error"]["code"])
        with self.extension["database"].session() as session:
            self.assertEqual("failed", session.get(DispatchRun, run["id"]).status)

    def test_source_selection_change_fences_finalization(self):
        session_id, source_id = self._source()
        newer_source_id = self._stage_artifact(
            session_id,
            name="newer-source.srt",
            role="transcription",
            text="A different transcript.",
            parent_id=source_id,
        )
        with self.extension["database"].session() as session:
            choose_artifact(session, session_id, "transcribe", source_id)
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"], "source-selection-claim")
        with self.extension["database"].session() as session:
            choose_artifact(session, session_id, "transcribe", newer_source_id)
        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "response_text": '{"operations": []}',
            },
            headers=self._headers("source-selection-submit"),
        )
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("finalization_conflict", response.get_json()["error"]["code"])
        self.assertEqual(
            ["transcribe"],
            response.get_json()["error"]["details"]["changed_stage_keys"],
        )

    def test_output_selection_change_fences_finalization(self):
        session_id, source_id = self._source()
        correction_one_id = self._stage_artifact(
            session_id,
            name="correction-one.srt",
            role="correction",
            text="First correction.",
            parent_id=source_id,
        )
        correction_two_id = self._stage_artifact(
            session_id,
            name="correction-two.srt",
            role="correction",
            text="Second correction.",
            parent_id=source_id,
        )
        with self.extension["database"].session() as session:
            choose_artifact(session, session_id, "correct", correction_one_id)
        run = self._create(session_id, source_artifact_id=source_id)
        claim = self._claim(run["id"], "output-selection-claim")
        with self.extension["database"].session() as session:
            choose_artifact(session, session_id, "correct", correction_two_id)
        response = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "response_text": '{"operations": []}',
            },
            headers=self._headers("output-selection-submit"),
        )
        self.assertEqual(409, response.status_code, response.get_json())
        self.assertEqual("finalization_conflict", response.get_json()["error"]["code"])
        self.assertEqual(
            ["correct"],
            response.get_json()["error"]["details"]["changed_stage_keys"],
        )


if __name__ == "__main__":
    unittest.main()
