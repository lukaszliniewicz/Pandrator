"""Continuation previews include only stages the executor may run."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from unittest.mock import patch

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import OutcomePlan


class WorkflowPlanStageScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.extension = self.app.extensions["pandrator"]
        self.services = self.extension["services"]
        self.addCleanup(self.extension["database"].dispose)

    def _session(self, included_stages: list[str]) -> str:
        return self.services.sessions.create(
            f"Plan stage scope {uuid.uuid4().hex[:8]}",
            workflow_kind="voiceover",
            included_stages=included_stages,
        ).id

    def _set_outcome(
        self,
        session_id: str,
        *,
        inputs: dict[str, str],
        transformations: dict[str, bool],
    ) -> None:
        with self.extension["database"].session() as db_session:
            plan = db_session.get(OutcomePlan, session_id)
            if plan is None:
                plan = OutcomePlan(session_id=session_id, value_json={})
                db_session.add(plan)
            plan.value_json = {
                "inputs": inputs,
                "transformations": transformations,
            }

    def _primary_source(self, session_id: str, suffix: str) -> str:
        path = self.services.paths.uploads / f"{uuid.uuid4().hex}-source.{suffix}"
        if suffix in {"srt", "vtt"}:
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nStage scope fixture.\n",
                encoding="utf-8",
            )
        else:
            path.write_bytes(b"bounded stage scope fixture")
        artifact = self.services.artifacts.register(
            path,
            kind=suffix,
            role="upload",
            session_id=session_id,
            metadata={"original_filename": path.name},
        )
        asset = self.services.source_library.ensure_for_artifact(
            artifact.id,
            display_name=path.name,
            kind=suffix,
        )
        self.services.source_library.attach(session_id, asset.id)
        return artifact.id

    @staticmethod
    def _snapshot_stages(
        included_stages: set[str], statuses: dict[str, str] | None = None
    ) -> list[dict[str, object]]:
        statuses = statuses or {}
        keys = ("transcribe", "correct", "translate", "generate_audio", "export")
        return [
            {
                "key": key,
                "title": key.replace("_", " ").title(),
                "included": key in included_stages,
                "executable": True,
                "status": statuses.get(key, "ready"),
                "selected_artifact_id": (
                    f"{key}-selected" if statuses.get(key) == "completed" else None
                ),
            }
            for key in keys
        ]

    def _ordered_steps(
        self,
        session_id: str,
        included_stages: set[str],
        *,
        statuses: dict[str, str] | None = None,
        reuse_stages: set[str] | None = None,
        mismatched_stages: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        snapshot = {"stages": self._snapshot_stages(included_stages, statuses)}
        with patch.object(self.services.workflows, "snapshot", return_value=snapshot):
            return self.extension["workflow_plans"]._ordered_steps(
                session_id=session_id,
                target_stage="export",
                resolved=None,
                reuse_stages=reuse_stages or set(),
                mismatched_stages=mismatched_stages,
            )

    def test_source_voiceover_omits_disabled_text_stages_but_keeps_wav_transcribe(self) -> None:
        included = {"generate_audio", "export"}
        session_id = self._session(sorted(included))
        self._primary_source(session_id, "wav")
        self._set_outcome(
            session_id,
            inputs={"generation": "source", "translation": "source"},
            transformations={"correct": False, "translate": False},
        )

        steps = self._ordered_steps(session_id, included)

        self.assertEqual(
            ["transcribe", "generate_audio", "export"],
            [step["stage"] for step in steps],
        )
        self.assertEqual("export", steps[-1]["stage"])

    def test_explicit_translation_and_correction_inputs_add_unincluded_prerequisites(self) -> None:
        included = {"generate_audio", "export"}
        session_id = self._session(sorted(included))
        self._primary_source(session_id, "wav")
        self._set_outcome(
            session_id,
            inputs={"generation": "translation", "translation": "correction"},
            transformations={"correct": False, "translate": False},
        )

        steps = self._ordered_steps(session_id, included)
        stages = [step["stage"] for step in steps]

        self.assertIn("correct", stages)
        self.assertIn("translate", stages)
        self.assertEqual("export", stages[-1])

    def test_included_text_stages_remain_even_when_transformations_are_false(self) -> None:
        included = {"correct", "translate", "generate_audio", "export"}
        session_id = self._session(sorted(included))
        self._primary_source(session_id, "wav")
        self._set_outcome(
            session_id,
            inputs={"generation": "source", "translation": "source"},
            transformations={"correct": False, "translate": False},
        )

        steps = self._ordered_steps(session_id, included)

        self.assertEqual(
            ["transcribe", "correct", "translate", "generate_audio", "export"],
            [step["stage"] for step in steps],
        )
        self.assertEqual("export", steps[-1]["stage"])

    def test_subtitle_primary_source_does_not_require_transcription(self) -> None:
        included = {"generate_audio", "export"}
        session_id = self._session(sorted(included))
        self._primary_source(session_id, "srt")
        self._set_outcome(
            session_id,
            inputs={"generation": "source", "translation": "source"},
            transformations={"correct": False, "translate": False},
        )

        steps = self._ordered_steps(session_id, included)

        self.assertEqual(
            ["generate_audio", "export"],
            [step["stage"] for step in steps],
        )
        self.assertEqual("export", steps[-1]["stage"])

    def test_reuse_and_mismatches_only_affect_allowed_stages(self) -> None:
        included = {"correct", "generate_audio", "export"}
        session_id = self._session(sorted(included))
        self._primary_source(session_id, "wav")
        self._set_outcome(
            session_id,
            inputs={"generation": "source", "translation": "source"},
            transformations={"correct": False, "translate": False},
        )

        steps = self._ordered_steps(
            session_id,
            included,
            statuses={
                "transcribe": "completed",
                "correct": "completed",
                "translate": "completed",
                "generate_audio": "completed",
            },
            reuse_stages={"correct", "translate"},
            mismatched_stages={
                "transcribe": {"reasons": ["changed source"]},
                "translate": {"reasons": ["changed settings"]},
            },
        )

        by_stage = {step["stage"]: step for step in steps}
        self.assertEqual(
            ["transcribe", "correct", "generate_audio", "export"],
            [step["stage"] for step in steps],
        )
        self.assertEqual("run", by_stage["transcribe"]["decision"])
        self.assertEqual("reuse_explicit", by_stage["correct"]["decision"])
        self.assertEqual("reuse_if_unchanged", by_stage["generate_audio"]["decision"])
        self.assertNotIn("translate", by_stage)
        self.assertEqual("export", steps[-1]["stage"])
