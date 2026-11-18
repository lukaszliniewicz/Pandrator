"""Post-correction filler diagnostics against the declared removal policy."""

import tempfile
import unittest
from types import SimpleNamespace

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.filler_check import filler_check
from pandrator.web.models import Artifact
from pandrator_mcp.schemas import GetDispatchRunInput
from pandrator_mcp.tools.dispatch import _metadata, get_dispatch_run
from tests.web_test_support import prepare_web_test_data_root


class FillerCheckUnitTests(unittest.TestCase):
    def _values(self, *texts):
        return [{"text": text, "start_ms": index * 1_000, "end_ms": index * 1_000 + 900}
                for index, text in enumerate(texts)]

    def test_two_remaining_fillers_are_listed(self):
        check = filler_check(
            self._values("Uh, hello there.", "Um, goodbye.", "Nothing here."),
            kind="correction",
            document_language="en",
            settings={"correction_style": "publishable"},
        )

        self.assertEqual("fillers_remaining", check["status"])
        self.assertEqual("publishable", check["policy"])
        self.assertEqual(2, check["count"])
        self.assertEqual(
            [{"cue_id": 1, "text": "Uh, hello there."}, {"cue_id": 2, "text": "Um, goodbye."}],
            check["passages"],
        )

    def test_clean_result_has_no_passages(self):
        check = filler_check(
            self._values("Hello there.", "Goodbye."),
            kind="correction",
            document_language="en",
            settings={"correction_style": "publishable"},
        )

        self.assertEqual("clean", check["status"])
        self.assertEqual(0, check["count"])
        self.assertEqual([], check["passages"])

    def test_non_english_document_is_not_checked(self):
        check = filler_check(
            self._values("Äh, hallo.", "Also, tschüss."),
            kind="correction",
            document_language="de",
            settings={"correction_style": "publishable"},
        )

        self.assertEqual("not_checked", check["status"])
        self.assertEqual("publishable", check["policy"])
        self.assertEqual([], check["passages"])

    def test_faithful_style_never_scans(self):
        check = filler_check(
            self._values("Uh, hello there."),
            kind="correction",
            document_language="en",
            settings={"correction_style": "faithful"},
        )

        self.assertEqual("not_checked", check["status"])
        self.assertEqual("faithful", check["policy"])
        self.assertEqual([], check["passages"])

    def test_translation_runs_have_no_filler_check(self):
        self.assertIsNone(
            filler_check(
                self._values("Uh, hello."),
                kind="translation",
                document_language="en",
                settings={"correction_style": "publishable"},
            )
        )

    def test_only_whole_tokens_match(self):
        check = filler_check(
            self._values("Uhh, term of art, and erm... yes.", "Ah. Hmm, quite."),
            kind="correction",
            document_language="en-GB",
            settings={"correction_style": "publishable"},
        )

        self.assertEqual(3, check["count"])
        self.assertEqual(2, len(check["passages"]))

    def test_logical_passages_are_named_by_their_passage_id(self):
        check = filler_check(
            [{"id": "p000003", "text": "Uh, hello.", "source_cue_ids": ["cue-77", "cue-78"]}],
            kind="correction",
            document_language="en",
            settings={"correction_style": "publishable"},
            logical=True,
        )

        self.assertEqual([{"passage_id": "p000003", "text": "Uh, hello."}], check["passages"])

    def test_display_cues_are_named_by_their_ordinal_even_with_an_id(self):
        check = filler_check(
            [{"id": "segment-uuid", "text": "Uh, hello.", "source_cue_ids": ["cue-77"]}],
            kind="correction",
            document_language="en",
            settings={"correction_style": "publishable"},
        )

        self.assertEqual([{"cue_id": 1, "text": "Uh, hello."}], check["passages"])


class FillerCheckMcpProjectionTests(unittest.TestCase):
    def test_run_metadata_keeps_the_check_and_points_at_the_patch_tool(self):
        payload = {
            "id": "run-1",
            "session_id": "session-1",
            "kind": "correction",
            "status": "completed",
            "result_artifact_id": "artifact-1",
            "filler_check": {
                "status": "fillers_remaining",
                "policy": "publishable",
                "count": 2,
                "passages": [{"cue_id": 77, "text": "Uh, hello."}],
            },
        }
        runtime = SimpleNamespace(
            require_application=lambda: SimpleNamespace(
                get_dispatch_run=lambda run_id: payload
            )
        )

        outcome = get_dispatch_run(runtime, GetDispatchRunInput(run_id="run-1"))

        self.assertEqual(payload["filler_check"], outcome.result["filler_check"])
        self.assertEqual("pandrator_patch_subtitle_passages", outcome.next_actions[0].tool)
        self.assertEqual("session-1", outcome.next_actions[0].arguments["session_id"])
        self.assertEqual("artifact-1", outcome.next_actions[0].arguments["source_artifact_id"])

    def test_clean_check_adds_no_action(self):
        payload = {
            "id": "run-1",
            "kind": "correction",
            "status": "completed",
            "filler_check": {"status": "clean", "policy": "publishable", "count": 0, "passages": []},
        }
        runtime = SimpleNamespace(
            require_application=lambda: SimpleNamespace(
                get_dispatch_run=lambda run_id: payload
            )
        )

        outcome = get_dispatch_run(runtime, GetDispatchRunInput(run_id="run-1"))

        self.assertEqual([], outcome.next_actions)
        self.assertEqual("clean", _metadata(payload)["filler_check"]["status"])


class FillerCheckReceiptTests(unittest.TestCase):
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

    def _headers(self, key=None):
        headers = {"X-CSRF-Token": self.csrf}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def _source(self, *, texts, source_language="en"):
        session = self.extension["sessions"].create(
            "Filler",
            workflow_kind="subtitles",
            source_language=source_language,
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
            language=source_language,
        )
        return session.id, artifact.id

    def _correct(self, *, texts, source_language="en", correction_style=None):
        session_id, source_id = self._source(texts=texts, source_language=source_language)
        body = {"kind": "correction", "source_artifact_id": source_id}
        if correction_style is not None:
            body["correction_style"] = correction_style
        created = self.client.post(
            f"/api/v1/sessions/{session_id}/dispatch-runs",
            json=body,
            headers=self._headers(),
        )
        self.assertEqual(201, created.status_code, created.get_json())
        run = created.get_json()
        claim = self.client.post(
            f"/api/v1/dispatch-runs/{run['id']}/claim",
            json={},
            headers=self._headers("claim-filler-1"),
        ).get_json()
        submitted = self.client.post(
            f"/api/v1/dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {"kind": "correction", "operations": []},
            },
            headers=self._headers("submit-filler-1"),
        )
        self.assertEqual(200, submitted.status_code, submitted.get_json())
        fetched = self.client.get(f"/api/v1/dispatch-runs/{run['id']}")
        self.assertEqual(200, fetched.status_code, fetched.get_json())
        return submitted.get_json(), fetched.get_json()

    def test_two_remaining_fillers_reach_both_receipts(self):
        submitted, fetched = self._correct(
            texts=("Uh, hello there.", "Um, goodbye.")
        )

        self.assertEqual("fillers_remaining", submitted["filler_check"]["status"])
        self.assertEqual(2, submitted["filler_check"]["count"])
        self.assertEqual(
            ["Uh, hello there.", "Um, goodbye."],
            [passage["text"] for passage in submitted["filler_check"]["passages"]],
        )
        self.assertEqual("fillers_remaining", fetched["filler_check"]["status"])
        self.assertEqual(2, len(fetched["filler_check"]["passages"]))
        with self.extension["database"].session() as session:
            artifact = session.get(Artifact, submitted["result_artifact_id"])
            self.assertEqual(
                "fillers_remaining", artifact.metadata_json["filler_check"]["status"]
            )

    def test_a_reported_passage_can_be_patched_by_its_id(self):
        submitted, _ = self._correct(texts=("Hello there.", "Um, goodbye."))
        artifact_id = submitted["result_artifact_id"]
        [reported] = submitted["filler_check"]["passages"]
        with self.extension["database"].session() as session:
            session_id = session.get(Artifact, artifact_id).session_id
        review = self.client.get(
            f"/api/v1/sessions/{session_id}/subtitles/review",
            query_string={"artifact_id": artifact_id},
        ).get_json()
        [column] = [item for item in review["columns"] if item["artifact_id"] == artifact_id]
        # As pandrator_patch_subtitle_passages sends it: every passage, one edited.
        passages = [
            {key: row[key] for key in ("id", "text", "speaker", "start_ms", "end_ms") if key in row}
            for row in column["logical_passages"]
        ]
        [edited] = [row for row in passages if row["id"] == reported["passage_id"]]
        edited["text"] = "Goodbye."

        patched = self.client.post(
            f"/api/v1/sessions/{session_id}/subtitles/{column['stage']}/passage-review",
            json={
                "source_artifact_id": artifact_id,
                "expected_source_hash": column["source_content_hash"],
                "expected_revision": column["revision"],
                "expected_composition_hash": column["composition_hash"],
                "passages": passages,
                "dry_run": True,
            },
            headers=self._headers("patch-filler-1"),
        )

        self.assertEqual(200, patched.status_code, patched.get_json())

    def test_clean_correction_reports_clean(self):
        submitted, fetched = self._correct(texts=("Hello there.", "Goodbye."))

        self.assertEqual("clean", submitted["filler_check"]["status"])
        self.assertEqual(0, submitted["filler_check"]["count"])
        self.assertEqual([], fetched["filler_check"]["passages"])

    def test_non_english_document_is_not_checked(self):
        submitted, _ = self._correct(
            texts=("Äh, hallo.", "Also, tschüss."), source_language="de"
        )

        self.assertEqual("not_checked", submitted["filler_check"]["status"])
        self.assertEqual("publishable", submitted["filler_check"]["policy"])

    def test_faithful_style_is_not_checked_and_names_the_policy(self):
        submitted, _ = self._correct(
            texts=("Uh, hello there.",), correction_style="faithful"
        )

        self.assertEqual("not_checked", submitted["filler_check"]["status"])
        self.assertEqual("faithful", submitted["filler_check"]["policy"])
        self.assertEqual(0, submitted["filler_check"]["count"])


if __name__ == "__main__":
    unittest.main()
