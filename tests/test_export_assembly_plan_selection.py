"""Isolated compatibility check: active-mix vs selected-history plan selection.

Scenario: completed run R1 refers to the ORIGINAL plan revision P1, while the
newer editable copy P2 is active and the latest assembled audio was built from
P2. The server-side export chain (export.variant ->
_ensure_export_generation_assembly -> export) must keep every step scoped to
the run the user selected: selecting R1 assembles/exports P1 audio, selecting
R2 uses P2 audio. It must never substitute the other run's assembly (older or
newer text) through settings-hash reuse.

DB rows only: no ffmpeg, no synthesis, no live data. The reuse path returns
before any audio is touched; the creation path is asserted via the pinned
assembly row (no takes exist, so assembly itself raises).
"""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from sqlalchemy import select

from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    GenerationPlan,
    GenerationRun,
    OutputAssembly,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import output_assembly_settings_hash
from tests.web_test_support import prepare_web_test_data_root


def _plan_records(*texts):
    return [{"text": text} for text in texts]


class ExportAssemblyPlanSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = __import__("tempfile").TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.record = SessionService(self.database).create(
            "Plan selection", workflow_kind="voiceover"
        )
        (self.paths.sessions / self.record.storage_key).mkdir()
        # Original plan P1, then an edited newer copy P2 (now active).
        self.revision_p1, _ = self.handlers._store_generation_plan(
            self.record.id, _plan_records("Original passage text."),
            settings={}, source_revision_id=None,
        )
        self.revision_p2, _ = self.handlers._store_generation_plan(
            self.record.id, _plan_records("Edited newer passage text."),
            settings={}, source_revision_id=None,
        )
        self.assertNotEqual(self.revision_p1, self.revision_p2)
        with self.database.session() as session:
            plan = session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == self.record.id
                )
            )
            self.assertEqual(plan.active_revision_id, self.revision_p2)
            run_old = GenerationRun(
                session_id=self.record.id,
                sequence_number=1,
                plan_revision_id=self.revision_p1,
                status="completed",
                settings_snapshot_json={},
            )
            run_new = GenerationRun(
                session_id=self.record.id,
                sequence_number=2,
                plan_revision_id=self.revision_p2,
                status="completed",
                settings_snapshot_json={},
            )
            session.add(run_old)
            session.add(run_new)
            session.flush()
            self.run_old_id = run_old.id
            self.run_new_id = run_new.id
            self.artifact_old_id = self._artifact(session, "assembly-old.wav")
            self.artifact_new_id = self._artifact(session, "assembly-new.wav")

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _artifact(self, session, name):
        artifact = Artifact(
            session_id=self.record.id,
            kind="audio",
            role="assembled_audio",
            relative_path=name,
            state="current",
        )
        session.add(artifact)
        session.flush()
        return artifact.id

    def _assembly(self, session, run_id, revision_id, snapshot, artifact_id):
        row = OutputAssembly(
            session_id=self.record.id,
            generation_run_id=run_id,
            status="completed",
            artifact_id=artifact_id,
            settings_json={
                "resolved": dict(snapshot),
                "plan_revision_id": revision_id,
            },
            settings_hash=output_assembly_settings_hash(snapshot),
        )
        session.add(row)
        session.flush()
        return row.id

    def _ensure(self, run_id, snapshot):
        return self.handlers._ensure_export_generation_assembly(
            session_id=self.record.id,
            generation_run_id=run_id,
            resolved_settings_snapshot=snapshot,
            progress=lambda *_: None,
            cancel_event=threading.Event(),
        )

    def test_reuse_is_scoped_to_selected_run_not_latest_assembly(self):
        snapshot = {"audio": {"format": "wav"}, "output": {"export_mode": "media"}}
        with self.database.session() as session:
            # Same output settings hash, but bound to different runs/plans:
            # A_new is the "latest assembled audio" from the newer copy P2.
            self._assembly(
                session, self.run_old_id, self.revision_p1,
                snapshot, self.artifact_old_id,
            )
            self._assembly(
                session, self.run_new_id, self.revision_p2,
                snapshot, self.artifact_new_id,
            )
        # Selecting the older run must reuse the older assembly, never the
        # newer text; selecting the newer run must reuse the newer assembly,
        # never the older text.
        self.assertEqual(self._ensure(self.run_old_id, snapshot), self.artifact_old_id)
        self.assertEqual(self._ensure(self.run_new_id, snapshot), self.artifact_new_id)

    def test_creation_pins_selected_run_plan_not_active_mix(self):
        snapshot = {"audio": {"format": "wav"}, "output": {"export_mode": "media"}}
        other = {"audio": {"format": "wav"}, "output": {"export_mode": "audio"}}
        other_hash = output_assembly_settings_hash(other)
        with self.database.session() as session:
            self._assembly(
                session, self.run_new_id, self.revision_p2,
                snapshot, self.artifact_new_id,
            )
        # No matching assembly for the older run under the new settings: the
        # chain must create one bound to R1/P1 (assembly itself raises here
        # because the fixture has no takes) rather than reusing the newer
        # assembly or the active mix.
        with self.assertRaises(ValueError):
            self._ensure(self.run_old_id, other)
        with self.database.session() as session:
            created = session.scalars(
                select(OutputAssembly)
                .where(
                    OutputAssembly.session_id == self.record.id,
                    OutputAssembly.generation_run_id == self.run_old_id,
                    OutputAssembly.settings_hash == other_hash,
                )
                .order_by(OutputAssembly.created_at.desc())
            ).first()
            assert created is not None
            self.assertEqual(
                (created.settings_json or {}).get("plan_revision_id"),
                self.revision_p1,
            )

    def test_variant_pins_the_ensured_assembly_into_export(self):
        snapshot = {
            "audio": {"format": "wav"},
            "output": {"export_mode": "audio"},
        }
        with self.database.session() as session:
            self._assembly(
                session,
                self.run_old_id,
                self.revision_p1,
                snapshot,
                self.artifact_old_id,
            )

        with mock.patch.object(
            self.handlers,
            "export",
            return_value={"artifact_ids": []},
        ) as export:
            self.handlers.handler_registry["export.variant"](
                {
                    "session_id": self.record.id,
                    "settings": {
                        "generation_run_id": self.run_old_id,
                        "export_mode": "audio",
                        "audio_mode": "dubbing_only",
                    },
                    "resolved_settings_snapshot": snapshot,
                },
                lambda *_args: None,
                threading.Event(),
            )

        export_payload = export.call_args.args[0]
        self.assertEqual(
            self.artifact_old_id,
            export_payload["pinned_assembly_artifact_id"],
        )

    def test_variant_canceled_during_inline_assembly_never_starts_export(self):
        canceled = threading.Event()
        assembly_ids = []

        def cancel_assembly(payload, _progress, cancel_event):
            self.assertIs(canceled, cancel_event)
            assembly_ids.append(payload["output_assembly_id"])
            cancel_event.set()
            return {}

        with (
            mock.patch.object(
                self.handlers, "assemble_generation_output", side_effect=cancel_assembly
            ),
            mock.patch.object(self.handlers, "export") as export,
        ):
            result = self.handlers.handler_registry["export.variant"](
                {
                    "session_id": self.record.id,
                    "settings": {
                        "generation_run_id": self.run_old_id,
                        "export_mode": "audio",
                        "audio_mode": "dubbing_only",
                    },
                    "resolved_settings_snapshot": {},
                },
                lambda *_args: None,
                canceled,
            )

        self.assertEqual({}, result)
        export.assert_not_called()
        self.assertEqual(1, len(assembly_ids))
        with self.database.session() as session:
            assembly = session.get(OutputAssembly, assembly_ids[0])
            self.assertEqual(self.run_old_id, assembly.generation_run_id)
            self.assertEqual(self.revision_p1, assembly.settings_json["plan_revision_id"])


if __name__ == "__main__":
    unittest.main()
