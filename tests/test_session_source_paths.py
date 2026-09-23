"""Source identity, lifecycle serialization and managed-path contracts."""

import tempfile
import threading
import unittest
from pathlib import Path

from sqlalchemy import event, func, select

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    OutcomePlan,
    OutcomePlanHistory,
    SessionSource,
    SourceAsset,
)
from pandrator.web.workspace import OutcomePlanService, RevisionConflict
from tests.web_test_support import prepare_web_test_data_root


class SessionSourcePathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Source paths", "workflow_kind": "audiobook"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        self.session_id = response.get_json()["id"]
        self.extension = self.app.extensions["pandrator"]
        self.path = Path(self.temporary.name) / "Zażółć źródło (1).txt"
        self.path.write_text("A managed source file.", encoding="utf-8")
        artifact = ArtifactService(
            self.extension["database"], self.extension["paths"]
        ).register(
            self.path,
            kind="source",
            role="upload",
            session_id=self.session_id,
        )
        self.artifact_id = artifact.id
        asset = self.extension["source_library"].ensure_for_artifact(
            artifact.id, display_name=self.path.name, kind="txt"
        )
        self.asset_id = asset.id
        self.extension["source_library"].attach(self.session_id, asset.id)

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def source(self):
        response = self.client.get(
            f"/api/v1/sessions/{self.session_id}/sources"
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()["items"][0]

    def _new_artifact(self, name):
        path = Path(self.temporary.name) / name
        path.write_text(name, encoding="utf-8")
        return ArtifactService(self.extension["database"], self.extension["paths"]).register(
            path, kind="txt", role="upload", session_id=self.session_id,
        )

    def _new_asset(self, name):
        artifact = self._new_artifact(name)
        return self.extension["source_library"].ensure_for_artifact(artifact.id)

    def _compete(self, first_action, second_action, statement_prefix):
        database = self.extension["database"]
        first_writing = threading.Event()
        second_started = threading.Event()
        second_finished = threading.Event()
        results = {}

        def before_write(_connection, _cursor, statement, _parameters, _context, _many):
            if threading.current_thread().name == "lifecycle-first" and statement.startswith(statement_prefix):
                first_writing.set()
                if not second_started.wait(5):
                    raise AssertionError("The competing writer did not start.")
                # In the broken read/check/write path the competing commit fits
                # here. An immediate transaction keeps it waiting until commit.
                second_finished.wait(1)

        def run(name, action):
            try:
                if name == "second":
                    if not first_writing.wait(5):
                        raise AssertionError("The first writer did not reach its write.")
                    second_started.set()
                results[name] = action()
            except Exception as error:
                results[name] = error
            finally:
                if name == "second":
                    second_finished.set()

        first = threading.Thread(target=run, args=("first", first_action), name="lifecycle-first")
        second = threading.Thread(target=run, args=("second", second_action), name="lifecycle-second")
        event.listen(database.engine, "before_cursor_execute", before_write)
        try:
            first.start()
            second.start()
            first.join(10)
            second.join(10)
            self.assertFalse(first.is_alive() or second.is_alive(), "Lifecycle writers did not finish.")
        finally:
            event.remove(database.engine, "before_cursor_execute", before_write)
        self.assertTrue(first_writing.is_set())
        return results

    def test_competing_outcome_updates_preserve_one_revision_and_history(self):
        service = OutcomePlanService(self.extension["database"])
        initial = service.get(self.session_id)
        first = {**initial["value"], "focus": "first"}
        second = {**initial["value"], "focus": "second"}
        results = self._compete(
            lambda: service.update(self.session_id, initial["revision"], first),
            lambda: service.update(self.session_id, initial["revision"], second),
            "UPDATE outcome_plans ",
        )
        self.assertIsInstance(results["first"], dict)
        self.assertIsInstance(results["second"], RevisionConflict)
        self.assertEqual(first, service.get(self.session_id)["value"])
        with self.extension["database"].session() as session:
            history = list(session.scalars(select(OutcomePlanHistory).where(
                OutcomePlanHistory.session_id == self.session_id,
            )))
            self.assertEqual(1, len(history))
            self.assertEqual(initial["value"], history[0].value_json)

    def test_competing_outcome_initialization_returns_the_same_plan(self):
        database = self.extension["database"]
        with database.session() as session:
            existing = session.get(OutcomePlan, self.session_id)
            if existing is not None:
                session.delete(existing)
        service = OutcomePlanService(database)
        results = self._compete(
            lambda: service.get(self.session_id), lambda: service.get(self.session_id),
            "INSERT INTO outcome_plans ",
        )
        self.assertIsInstance(results["first"], dict)
        self.assertEqual(results["first"], results["second"])

    def test_competing_source_promotion_preserves_one_asset_identity(self):
        artifact = self._new_artifact("promotion.txt")
        service = self.extension["source_library"]
        results = self._compete(
            lambda: service.ensure_for_artifact(artifact.id),
            lambda: service.ensure_for_artifact(artifact.id),
            "INSERT INTO source_assets ",
        )
        self.assertIsInstance(results["first"], SourceAsset)
        self.assertIsInstance(results["second"], SourceAsset)
        self.assertEqual(results["first"].id, results["second"].id)
        with self.extension["database"].session() as session:
            self.assertEqual(1, session.scalar(select(func.count()).select_from(SourceAsset).where(
                SourceAsset.artifact_id == artifact.id,
            )))

    def test_competing_source_attachments_leave_one_current_primary(self):
        first, second = self._new_asset("first.txt"), self._new_asset("second.txt")
        service = self.extension["source_library"]
        results = self._compete(
            lambda: service.attach(self.session_id, first.id),
            lambda: service.attach(self.session_id, second.id),
            "UPDATE session_sources ",
        )
        self.assertIsInstance(results["first"], dict)
        self.assertIsInstance(results["second"], dict)
        current = [item for item in service.list(session_id=self.session_id) if item["attachment"]["is_current"]]
        self.assertEqual([second.id], [item["id"] for item in current])
        self.assertTrue(self.path.is_file())

    def test_competing_source_renames_reject_stale_revision(self):
        service = self.extension["source_library"]
        results = self._compete(
            lambda: service.rename(self.asset_id, 1, "First name"),
            lambda: service.rename(self.asset_id, 1, "Second name"),
            "UPDATE source_assets ",
        )
        self.assertIsInstance(results["first"], dict)
        self.assertIsInstance(results["second"], RevisionConflict)
        self.assertEqual("First name", self.source()["display_name"])

    def test_trashed_source_requires_restore_before_attachment(self):
        service = self.extension["source_library"]
        asset = self._new_asset("trashed.txt")
        trashed = service.set_state(asset.id, asset.revision, "trashed")
        with self.assertRaisesRegex(ValueError, "[Rr]estore"):
            service.attach(self.session_id, asset.id)
        self.assertEqual(self.asset_id, self.source()["id"])
        service.set_state(asset.id, trashed["revision"], "current")
        self.assertEqual(asset.id, service.attach(self.session_id, asset.id)["source_asset_id"])

    def test_trash_and_attach_cannot_leave_a_trashed_source_attached(self):
        service = self.extension["source_library"]
        asset = self._new_asset("trash-race.txt")
        results = self._compete(
            lambda: service.set_state(asset.id, asset.revision, "trashed"),
            lambda: service.attach(self.session_id, asset.id),
            "UPDATE source_assets ",
        )
        self.assertIsInstance(results["first"], dict)
        self.assertIsInstance(results["second"], ValueError)
        with self.extension["database"].session() as session:
            self.assertEqual("trashed", session.get(SourceAsset, asset.id).state)
            self.assertEqual(0, session.scalar(select(func.count()).select_from(SessionSource).where(
                SessionSource.source_asset_id == asset.id,
            )))

    def test_detach_and_reattach_preserve_the_new_current_attachment(self):
        service = self.extension["source_library"]
        other = self._new_asset("other.txt")
        service.attach(self.session_id, other.id)
        old = next(item for item in service.list(session_id=self.session_id) if item["id"] == self.asset_id)["attachment"]
        self.assertFalse(old["is_current"])
        results = self._compete(
            lambda: service.detach(self.session_id, old["id"], old["revision"]),
            lambda: service.attach(self.session_id, self.asset_id),
            "DELETE FROM session_sources ",
        )
        self.assertIsNone(results["first"])
        self.assertIsInstance(results["second"], dict)
        current = [item for item in service.list(session_id=self.session_id) if item["attachment"]["is_current"]]
        self.assertEqual([self.asset_id], [item["id"] for item in current])

    def _assert_missing_subtitle_artifact_is_rejected(self, method):
        # SourceAsset permits a null artifact reference (including ON DELETE
        # SET NULL). Retain the existing KeyError contract and atomic rollback.
        with self.extension["database"].session() as session:
            asset = session.get(SourceAsset, self.asset_id)
            asset.kind = "srt"
            asset.artifact_id = None
        before = self.source()
        with self.assertRaises(KeyError) as raised:
            getattr(self.extension["source_library"], method)(self.session_id, self.asset_id)
        self.assertEqual((None,), raised.exception.args)
        self.assertEqual(before, self.source())
        self.assertTrue(self.path.is_file())

    def test_attach_missing_subtitle_artifact_preserves_attachment(self):
        self._assert_missing_subtitle_artifact_is_rejected("attach")

    def test_adopt_missing_subtitle_artifact_preserves_attachment(self):
        self._assert_missing_subtitle_artifact_is_rejected("adopt_subtitles")

    def test_uploaded_source_exposes_absolute_unicode_path(self):
        source = self.source()
        self.assertIsNone(source["external_path"])
        self.assertEqual(str(self.path.resolve()), source["path"])
        self.assertEqual(self.artifact_id, source["artifact_id"])

    def test_original_location_is_distinct_from_managed_path(self):
        with self.extension["database"].session() as session:
            asset = session.get(SourceAsset, self.asset_id)
            asset.external_path = "/old/location/source.txt"
        source = self.source()
        self.assertEqual("/old/location/source.txt", source["external_path"])
        self.assertEqual(str(self.path.resolve()), source["path"])

    def test_invalid_legacy_path_does_not_break_source_list(self):
        with self.extension["database"].session() as session:
            artifact = session.get(Artifact, self.artifact_id)
            artifact.relative_path = "../outside-managed-root.txt"
        source = self.source()
        self.assertIsNone(source["path"])
        self.assertEqual(self.asset_id, source["id"])


if __name__ == "__main__":
    unittest.main()
