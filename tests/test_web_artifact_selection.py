import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


class WebArtifactSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Artifact paths", "workflow_kind": "voiceover"},
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        self.session_id = response.get_json()["id"]
        extension = self.app.extensions["pandrator"]
        self.artifacts = extension["artifacts"]
        self.paths = extension["paths"]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def artifact(self, name, role, text, parents=()):
        path = self.paths.sessions / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return self.artifacts.register(
            path,
            kind="srt" if name.endswith(".srt") else "source",
            role=role,
            session_id=self.session_id,
            parent_ids=list(parents),
            metadata={"original_filename": name} if role == "upload" else {},
        )

    def test_rerun_preserves_history_and_can_restore_the_old_lineage(self):
        source = self.artifact("source.mp4", "upload", "media")
        transcript_one = self.artifact("transcript-v1.srt", "transcription", "one", [source.id])
        correction_one = self.artifact("correction-v1.srt", "correction", "one!", [transcript_one.id])
        translation_one = self.artifact("translation-v1.srt", "translation", "uno", [correction_one.id])

        impact = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/impact"
        ).get_json()
        self.assertEqual(transcript_one.id, impact["selected_artifact"]["id"])
        self.assertEqual(["correct", "translate"], [item["stage_key"] for item in impact["dependent_selections"]])
        self.assertGreaterEqual(impact["descendant_total"], 2)

        transcript_two = self.artifact("transcript-v2.srt", "transcription", "two", [source.id])
        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        by_key = {item["key"]: item for item in workflow["stages"]}
        self.assertEqual(transcript_two.id, by_key["transcribe"]["selected_artifact_id"])
        self.assertIsNone(by_key["correct"]["selected_artifact_id"])
        self.assertEqual("stale", by_key["correct"]["status"])
        self.assertEqual(2, len(by_key["transcribe"]["artifacts"]))
        self.assertTrue((self.paths.sessions / "transcript-v1.srt").is_file())
        self.assertTrue((self.paths.sessions / "translation-v1.srt").is_file())

        translation_history = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/translate/artifacts"
        ).get_json()
        restored = self.client.put(
            f"/api/v1/sessions/{self.session_id}/stages/translate/selection",
            json={"artifact_id": translation_one.id},
            headers={**self.headers, "If-Match": f'"{translation_history["revision"]}"'},
        )
        self.assertEqual(200, restored.status_code, restored.get_json())
        self.assertEqual(transcript_one.id, restored.get_json()["restored"]["transcribe"])
        self.assertEqual(correction_one.id, restored.get_json()["restored"]["correct"])

        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        by_key = {item["key"]: item for item in workflow["stages"]}
        self.assertEqual(transcript_one.id, by_key["transcribe"]["selected_artifact_id"])
        self.assertEqual(correction_one.id, by_key["correct"]["selected_artifact_id"])
        self.assertEqual(translation_one.id, by_key["translate"]["selected_artifact_id"])

        queued = self.client.post(
            f"/api/v1/sessions/{self.session_id}/stages/correct/run",
            json={},
            headers=self.headers,
        )
        self.assertEqual(202, queued.status_code, queued.get_json())
        self.assertEqual(transcript_one.id, queued.get_json()["payload_json"]["source_artifact_id"])

    def test_clear_selection_keeps_history_but_unlocks_a_fresh_path(self):
        source = self.artifact("clear-source.mp4", "upload", "media")
        transcript = self.artifact("clear-transcript.srt", "transcription", "one", [source.id])
        correction = self.artifact("clear-correction.srt", "correction", "one!", [transcript.id])
        history = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts"
        ).get_json()
        response = self.client.put(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/selection",
            json={"artifact_id": None},
            headers={**self.headers, "If-Match": f'"{history["revision"]}"'},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        by_key = {item["key"]: item for item in workflow["stages"]}
        self.assertIsNone(by_key["transcribe"]["selected_artifact_id"])
        self.assertIsNone(by_key["correct"]["selected_artifact_id"])
        self.assertEqual(1, len(by_key["transcribe"]["artifacts"]))
        self.assertEqual(1, len(by_key["correct"]["artifacts"]))
        self.assertTrue((self.paths.sessions / "clear-transcript.srt").is_file())
        self.assertTrue((self.paths.sessions / "clear-correction.srt").is_file())

    def test_historical_leaf_can_be_trashed_but_selected_lineage_is_protected(self):
        source = self.artifact("trash-source.mp4", "upload", "media")
        transcript_one = self.artifact(
            "trash-transcript-v1.srt",
            "transcription",
            "one",
            [source.id],
        )
        correction_one = self.artifact(
            "trash-correction-v1.srt",
            "correction",
            "one!",
            [transcript_one.id],
        )
        transcript_two = self.artifact(
            "trash-transcript-v2.srt",
            "transcription",
            "two",
            [source.id],
        )

        selected = self.client.delete(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts/{transcript_two.id}",
            headers=self.headers,
        )
        self.assertEqual(409, selected.status_code, selected.get_json())

        has_descendant = self.client.delete(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts/{transcript_one.id}",
            headers=self.headers,
        )
        self.assertEqual(409, has_descendant.status_code, has_descendant.get_json())

        removed_child = self.client.delete(
            f"/api/v1/sessions/{self.session_id}/stages/correct/artifacts/{correction_one.id}",
            headers=self.headers,
        )
        self.assertEqual(200, removed_child.status_code, removed_child.get_json())
        self.assertTrue(removed_child.get_json()["file_retained"])
        self.assertTrue((self.paths.sessions / "trash-correction-v1.srt").is_file())

        removed_parent = self.client.delete(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts/{transcript_one.id}",
            headers=self.headers,
        )
        self.assertEqual(200, removed_parent.status_code, removed_parent.get_json())
        self.assertTrue((self.paths.sessions / "trash-transcript-v1.srt").is_file())

        history = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts"
        ).get_json()
        self.assertEqual(1, history["total"])
        self.assertEqual([transcript_two.id], [item["id"] for item in history["items"]])
        self.assertEqual(2, history["items"][0]["version"])
        operation = self.client.get("/api/v1/openapi.json").get_json()["paths"][
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/artifacts/{artifactId}"
        ]["delete"]
        self.assertIn(
            {"nativeOAuth": ["app.write"]},
            operation["security"],
        )

    def test_source_reattach_ignores_a_trashed_newer_result(self):
        source = self.artifact("reattach-source.mp4", "upload", "media")
        transcript_one = self.artifact(
            "reattach-transcript-v1.srt",
            "transcription",
            "one",
            [source.id],
        )
        transcript_two = self.artifact(
            "reattach-transcript-v2.srt",
            "transcription",
            "two",
            [source.id],
        )
        history = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts"
        ).get_json()
        selected = self.client.put(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/selection",
            json={"artifact_id": transcript_one.id},
            headers={
                **self.headers,
                "If-Match": f'"{history["revision"]}"',
            },
        )
        self.assertEqual(200, selected.status_code, selected.get_json())
        removed = self.client.delete(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts/{transcript_two.id}",
            headers=self.headers,
        )
        self.assertEqual(200, removed.status_code, removed.get_json())

        library = self.app.extensions["pandrator"]["source_library"]
        source_asset = library.ensure_for_artifact(
            source.id,
            display_name="reattach-source.mp4",
            kind="mp4",
        )
        attached = self.client.post(
            f"/api/v1/sessions/{self.session_id}/sources",
            json={"source_asset_id": source_asset.id},
            headers=self.headers,
        )
        self.assertEqual(201, attached.status_code, attached.get_json())
        workflow = self.client.get(
            f"/api/v1/sessions/{self.session_id}/workflow"
        ).get_json()
        transcribe = next(
            stage for stage in workflow["stages"] if stage["key"] == "transcribe"
        )
        self.assertEqual(transcript_one.id, transcribe["selected_artifact_id"])

    def test_stage_history_is_paginated_without_losing_version_numbers(self):
        source = self.artifact(
            "pagination-source.mp4",
            "upload",
            "media",
        )
        artifacts = [
            self.artifact(
                f"pagination-transcript-{index:03d}.srt",
                "transcription",
                str(index),
                [source.id],
            )
            for index in range(65)
        ]

        workflow = self.client.get(
            f"/api/v1/sessions/{self.session_id}/workflow"
        ).get_json()
        transcribe = next(
            item for item in workflow["stages"] if item["key"] == "transcribe"
        )
        self.assertEqual(65, transcribe["artifact_history_total"])
        self.assertEqual(10, len(transcribe["artifacts"]))
        self.assertTrue(transcribe["artifact_history_has_more"])
        self.assertEqual(
            artifacts[-1].id,
            transcribe["selected_artifact_id"],
        )
        self.assertEqual(
            list(range(65, 55, -1)),
            [item["version"] for item in transcribe["artifacts"]],
        )

        first_page = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts?limit=50"
        ).get_json()
        self.assertEqual(65, first_page["total"])
        self.assertEqual(50, len(first_page["items"]))
        self.assertEqual(16, first_page["next_before_version"])

        last_page = self.client.get(
            f"/api/v1/sessions/{self.session_id}/stages/transcribe/artifacts"
            f"?limit=50&before_version={first_page['next_before_version']}"
        ).get_json()
        self.assertEqual(15, len(last_page["items"]))
        self.assertFalse(last_page["has_more"])
        self.assertEqual(
            list(range(15, 0, -1)),
            [item["version"] for item in last_page["items"]],
        )

    def test_attached_library_source_is_exposed_to_the_workflow(self):
        global_path = self.paths.uploads / "library-source.srt"
        global_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(
            global_path,
            kind="source",
            role="upload",
            metadata={"original_filename": "library-source.srt"},
        )
        library = self.app.extensions["pandrator"]["source_library"]
        asset = library.ensure_for_artifact(source.id, display_name="library-source.srt", kind="srt")
        attached = self.client.post(
            f"/api/v1/sessions/{self.session_id}/sources",
            json={"source_asset_id": asset.id, "role": "primary"},
            headers=self.headers,
        )
        self.assertEqual(201, attached.status_code, attached.get_json())
        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        self.assertEqual(source.id, workflow["sources"][0]["id"])
        self.assertEqual("library-source.srt", workflow["sources"][0]["filename"])

    def test_switching_sources_clears_and_restores_their_compatible_path(self):
        first = self.artifact("first-source.mp4", "upload", "first")
        first_transcript = self.artifact("first-source.srt", "transcription", "one", [first.id])
        first_translation = self.artifact("first-translation.srt", "translation", "uno", [first_transcript.id])
        second = self.artifact("second-source.mp4", "upload", "second")
        library = self.app.extensions["pandrator"]["source_library"]
        first_asset = library.ensure_for_artifact(first.id, display_name="first-source.mp4", kind="mp4")
        second_asset = library.ensure_for_artifact(second.id, display_name="second-source.mp4", kind="mp4")

        library.attach(self.session_id, first_asset.id)
        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        by_key = {item["key"]: item for item in workflow["stages"]}
        self.assertEqual(first_transcript.id, by_key["transcribe"]["selected_artifact_id"])
        self.assertEqual(first_translation.id, by_key["translate"]["selected_artifact_id"])

        library.attach(self.session_id, second_asset.id)
        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        by_key = {item["key"]: item for item in workflow["stages"]}
        self.assertIsNone(by_key["transcribe"]["selected_artifact_id"])
        self.assertIsNone(by_key["translate"]["selected_artifact_id"])

        library.attach(self.session_id, first_asset.id)
        workflow = self.client.get(f"/api/v1/sessions/{self.session_id}/workflow").get_json()
        by_key = {item["key"]: item for item in workflow["stages"]}
        self.assertEqual(first_transcript.id, by_key["transcribe"]["selected_artifact_id"])
        self.assertEqual(first_translation.id, by_key["translate"]["selected_artifact_id"])


if __name__ == "__main__":
    unittest.main()
