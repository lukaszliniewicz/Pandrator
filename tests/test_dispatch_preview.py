import json
import tempfile
import unittest

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.dispatch_preview import get_dispatch_preview
from pandrator.web.models import DispatchBatch, DispatchRun
from tests.web_test_support import prepare_web_test_data_root


class DispatchPreviewTests(unittest.TestCase):
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
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()[
            "csrf_token"
        ]
        self.services = self.app.extensions["pandrator"]["services"]

    def tearDown(self):
        self.services.database.dispose()
        self.temporary.cleanup()

    def _headers(self, key: str):
        return {"X-CSRF-Token": self.csrf, "Idempotency-Key": key}

    def _source(
        self,
        texts: tuple[str, ...] = ("Hello world.", "Goodbye."),
        *,
        target_language: str | None = None,
    ):
        record = self.services.sessions.create(
            "Dispatch preview",
            workflow_kind="subtitles",
            source_language="en",
            target_language=target_language,
        )
        directory = self.services.paths.sessions / record.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        source_path = directory / "source.srt"
        source_path.write_text(
            "\n".join(
                f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\n{text}\n"
                for index, text in enumerate(texts, start=1)
            ),
            encoding="utf-8",
        )
        artifact = self.services.artifacts.register(
            source_path,
            kind="srt",
            role="transcription",
            session_id=record.id,
        )
        self.services.workflow_handlers._store_srt_document(
            record.id,
            artifact,
            "transcription",
            language="en",
        )
        return record.id

    def _create(self, session_id: str, **values):
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/dispatch-runs",
            json={"kind": "correction", **values},
            headers=self._headers("preview-create-key"),
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _claim(self, run_id: str, key: str):
        response = self.client.post(
            f"/api/v1/dispatch-runs/{run_id}/claim",
            json={},
            headers=self._headers(key),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def _submit_edit(
        self,
        claim: dict,
        *,
        text: str,
        key: str,
        uncertainty_reason: str | None = None,
    ):
        cue_id = claim["batch"]["cues"][0]["cue_id"]
        result = {
            "kind": "correction",
            "operations": [
                {
                    "action": "edit",
                    "cue_ids": [cue_id],
                    "texts": [text],
                }
            ],
        }
        if uncertainty_reason is not None:
            result["uncertainties"] = [
                {
                    "cue_id": cue_id,
                    "reason": uncertainty_reason,
                    "evidence_ids": [],
                }
            ]
        return self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": result,
            },
            headers=self._headers(key),
        )

    def test_logical_preview_pairs_persisted_output_to_accepted_source_cues(self):
        session_id = self._source()
        run = self._create(session_id, max_segments_per_batch=1)
        claim = self._claim(run["id"], "preview-logical-claim")
        submitted = self._submit_edit(
            claim,
            text="Edited first sentence.",
            key="preview-logical-submit",
            uncertainty_reason="Audio evidence remains ambiguous.",
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())

        preview = get_dispatch_preview(self.services, run["id"])

        self.assertEqual("1", preview["schema_version"])
        self.assertEqual(run["id"], preview["run_id"])
        self.assertEqual("correction", preview["kind"])
        self.assertEqual(run["source_revision_id"], preview["source_revision_id"])
        self.assertEqual(run["source_content_hash"], preview["source_content_hash"])
        self.assertEqual(claim["batch_id"], preview["batch_id"])
        self.assertEqual(1, preview["batch_ordinal"])
        self.assertTrue(preview["partial"])
        self.assertFalse(preview["published"])
        self.assertEqual("source_cue_ids", preview["source_pairing"])
        self.assertEqual(1, preview["output_count"])
        self.assertEqual(1, preview["total_count"])
        self.assertIsNone(preview["next_offset"])
        self.assertEqual("Edited first sentence.", preview["output_rows"][0]["text"])
        self.assertEqual([1], preview["output_rows"][0]["source_cue_ids"])
        self.assertEqual("uncertain", preview["output_rows"][0]["review_state"])
        self.assertEqual(
            "Audio evidence remains ambiguous.",
            preview["output_rows"][0]["review_note"],
        )
        self.assertEqual([], preview["output_rows"][0]["evidence_ids"])
        self.assertEqual([1], preview["output_rows"][0]["uncertain_source_cue_ids"])
        self.assertEqual([1], [cue["cue_id"] for cue in preview["source_cues"]])
        self.assertEqual("Hello world.", preview["source_cues"][0]["text"])
        self.assertEqual(0, preview["source_cues"][0]["timing"]["start_ms"])
        self.assertEqual(1000, preview["source_cues"][0]["timing"]["end_ms"])
        serialized = json.dumps(preview)
        self.assertNotIn("lease_token", serialized)
        self.assertNotIn("lease_expires_at", serialized)
        self.assertNotIn("operations", preview["output_rows"][0])

    def test_nonlogical_preview_returns_rows_without_fabricating_source_pairs(self):
        session_id = self._source(("An older stored run.",))
        run = self._create(session_id)
        with self.services.database.immediate_session() as session:
            stored_run = session.get(DispatchRun, run["id"])
            stored_run.settings_json = {
                key: value
                for key, value in dict(stored_run.settings_json or {}).items()
                if key != "_logical_passages_version"
            }

        claim = self._claim(run["id"], "preview-legacy-claim")
        submitted = self._submit_edit(
            claim,
            text="Corrected older run.",
            key="preview-legacy-submit",
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())

        preview = get_dispatch_preview(self.services, run["id"])

        self.assertTrue(preview["published"])
        self.assertEqual("unavailable", preview["source_pairing"])
        self.assertEqual([], preview["source_cues"])
        self.assertEqual("Corrected older run.", preview["output_rows"][0]["text"])
        self.assertNotIn("source_cue_ids", preview["output_rows"][0])

    def test_translation_preview_uses_persisted_normalized_rows(self):
        session_id = self._source(("A subtitle.",), target_language="pl")
        run = self._create(session_id, kind="translation", target_language="pl")
        claim = self._claim(run["id"], "preview-translation-claim")
        cue_id = claim["batch"]["cues"][0]["cue_id"]
        submitted = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "kind": "translation",
                    "translations": [
                        {"cue_id": cue_id, "text": "Napisy."},
                    ],
                },
            },
            headers=self._headers("preview-translation-submit"),
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())

        preview = get_dispatch_preview(self.services, run["id"])

        self.assertEqual("translation", preview["kind"])
        self.assertTrue(preview["published"])
        self.assertEqual("source_cue_ids", preview["source_pairing"])
        self.assertEqual("Napisy.", preview["output_rows"][0]["text"])
        self.assertEqual([cue_id], preview["output_rows"][0]["source_cue_ids"])
        self.assertEqual([cue_id], [cue["cue_id"] for cue in preview["source_cues"]])

    def test_unaccepted_batch_is_rejected_without_changing_lease_or_status(self):
        session_id = self._source()
        run = self._create(session_id, max_segments_per_batch=1)
        with self.assertRaisesRegex(ValueError, "no accepted batch"):
            get_dispatch_preview(self.services, run["id"])
        claim = self._claim(run["id"], "preview-partial-claim")
        submitted = self._submit_edit(
            claim,
            text="Accepted first sentence.",
            key="preview-partial-submit",
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())

        default_preview = get_dispatch_preview(self.services, run["id"])
        self.assertEqual(1, default_preview["batch_ordinal"])
        with self.assertRaisesRegex(ValueError, "not been accepted"):
            get_dispatch_preview(self.services, run["id"], batch_ordinal=2)

        with self.services.database.snapshot_session() as session:
            second_batch = session.scalar(
                select(DispatchBatch).where(
                    DispatchBatch.dispatch_run_id == run["id"],
                    DispatchBatch.ordinal == 1,
                )
            )
            self.assertEqual("ready", second_batch.status)
            self.assertIsNone(second_batch.lease_token)

    def test_pagination_limits_ordinals_and_unknown_runs(self):
        session_id = self._source()
        run = self._create(session_id)
        claim = self._claim(run["id"], "preview-page-claim")
        first_id = claim["batch"]["cues"][0]["cue_id"]
        second_id = claim["batch"]["cues"][1]["cue_id"]
        submitted = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "kind": "correction",
                    "operations": [
                        {
                            "action": "edit",
                            "cue_ids": [first_id],
                            "texts": ["First preview page."],
                        },
                        {
                            "action": "edit",
                            "cue_ids": [second_id],
                            "texts": ["Second preview page."],
                        },
                    ],
                },
            },
            headers=self._headers("preview-page-submit"),
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())

        first_page = get_dispatch_preview(self.services, run["id"], limit=1)
        second_page = get_dispatch_preview(self.services, run["id"], offset=1, limit=1)
        self.assertEqual(1, first_page["output_count"])
        self.assertEqual(2, first_page["total_count"])
        self.assertEqual(1, first_page["next_offset"])
        self.assertEqual(1, second_page["output_count"])
        self.assertIsNone(second_page["next_offset"])
        self.assertEqual("Second preview page.", second_page["output_rows"][0]["text"])

        for kwargs in (
            {"offset": -1},
            {"limit": 0},
            {"limit": 101},
            {"batch_ordinal": 0},
            {"batch_ordinal": True},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                get_dispatch_preview(self.services, run["id"], **kwargs)
        with self.assertRaises(KeyError):
            get_dispatch_preview(self.services, "missing-dispatch-run")


if __name__ == "__main__":
    unittest.main()
