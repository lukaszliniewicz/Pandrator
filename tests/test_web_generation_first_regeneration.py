"""First regeneration after a TTS-text edit: strict pin contract + adoption.

Live incident: inline/drawer edit of spoken_text saves (edit-copy R1 -> R2),
the first Regenerate click is rejected pre-enqueue with a plan-changed 409
while the segment remains stale, and a second click succeeds (pausing the
active run via the regeneration baton and resuming it afterwards).

Contract (strict, no pin rebinding on the backend):
- A stale explicit speech_plan_revision_id pin is ALWAYS rejected with 409,
  even when the segment IDs are already on the active revision. The pin is
  authoritative; the backend must not guess intent.
- The fix is authoritative edit-response adoption: mutation responses carry
  the new plan_revision_id (additive) plus previous_segment_id, and the UI
  store adopts both synchronously, so the first regenerate built from adopted
  state succeeds on the first try. A required save reload that loses a load
  race must not silently leave stale rows behind.
- Genuine concurrent edits (rows not on the active revision, or the active
  revision moving after the pin was read) still 409.

Uses fake TTS (silent audio), never real speech synthesis.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from unittest.mock import patch

from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, AudioTake, GenerationRun, Job
from pandrator.web.tts_providers import TtsBatchResult, TtsCapabilities


def _silent(text, _settings, **_kwargs):
    return AudioSegment.silent(duration=20)


def _silent_batch(items, **_kwargs):
    for item in items:
        yield TtsBatchResult(id=item.id, audio=AudioSegment.silent(duration=20))


def _capabilities(*_args, **_kwargs):
    return TtsCapabilities(
        batch_synthesis=False,
        streaming_batch=False,
        default_batch_size=1,
        max_batch_size=1,
    )


class FirstRegenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bootstrap = BootstrapTokenStore()
        token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.database = self.app.extensions["pandrator"]["database"]
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "First regen", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        self.session_id = created.get_json()["id"]
        plan = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan",
            json={"segments": [{"text": "One"}, {"text": "Two"}]},
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    # -- helpers ---------------------------------------------------------
    def _segments(self):
        response = self.client.get(
            f"/api/v1/sessions/{self.session_id}/generation-segments"
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def _patch_segment(self, segment_id, revision, changes, expected=200):
        response = self.client.patch(
            f"/api/v1/generation-segments/{segment_id}",
            json=changes,
            headers={**self.headers, "If-Match": f'"{revision}"'},
        )
        self.assertEqual(expected, response.status_code, response.get_json())
        return response.get_json()

    def _start_run(self, payload, expected=202):
        response = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-runs",
            json=payload,
            headers=self.headers,
        )
        self.assertEqual(expected, response.status_code, response.get_json())
        return response.get_json()

    def _mark_running(self, run_id):
        with self.database.session() as session:
            run = session.get(GenerationRun, run_id)
            run.status = "running"

    def _run_worker(self, run_id, operation="regenerate"):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        with self.database.session() as session:
            job = session.get(Job, session.get(GenerationRun, run_id).job_id)
            segment_ids = list((job.payload_json or {}).get("segment_ids") or [])
        with (
            patch.object(handlers.tts_providers, "synthesize", side_effect=_silent),
            patch.object(
                handlers.tts_providers, "synthesize_batch", side_effect=_silent_batch
            ),
            patch.object(
                handlers.tts_providers,
                "synthesis_capabilities",
                side_effect=_capabilities,
            ),
        ):
            return handlers.run_generation(
                {
                    "generation_run_id": run_id,
                    "segment_ids": segment_ids,
                    "operation": operation,
                },
                lambda _value, _detail=None: None,
                threading.Event(),
            )

    def _synthesized_texts(self, run_id):
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(Artifact).join(
                        AudioTake, AudioTake.artifact_id == Artifact.id
                    ).where(AudioTake.generation_run_id == run_id)
                )
            )
            return [(row.metadata_json or {}).get("synthesized_text") for row in rows]

    # -- the incident chain ----------------------------------------------
    def test_stale_pin_with_current_segments_still_rejected(self):
        """The strict pin contract: a stale pin always 409s, never rebinds."""
        page = self._segments()
        revision_r1 = page["plan_revision_id"]
        first = page["items"][0]

        main = self._start_run(
            {"operation": "generate", "run_override": {"tts": {"service": "openai"}}}
        )
        self._mark_running(main["id"])

        edited = self._patch_segment(
            first["id"], first["revision"], {"optimized_text": "One spoken"}
        )
        self.assertEqual(first["id"], edited.get("previous_segment_id"))
        revision_r2 = self._segments()["plan_revision_id"]
        self.assertNotEqual(revision_r1, revision_r2)
        # The mutation response itself carries the new authoritative revision.
        self.assertEqual(revision_r2, edited.get("plan_revision_id"))

        # A request mixing the stale R1 pin with current R2 IDs is rejected
        # pre-enqueue even though the IDs are current: the pin is binding.
        rejected = self._start_run(
            {
                "operation": "regenerate",
                "segment_ids": [edited["id"]],
                "speech_plan_revision_id": revision_r1,
            },
            expected=409,
        )
        self.assertIn("plan", rejected["error"]["message"].lower())
        # Nothing was enqueued; the edited segment is still stale.
        live = self._segments()
        self.assertEqual(
            "stale",
            next(i for i in live["items"] if i["id"] == edited["id"])["status"],
        )

    def test_adopted_response_regenerates_first_try(self):
        """UI-derived ordering: build regenerate purely from the save response."""
        page = self._segments()
        first = page["items"][0]

        main = self._start_run(
            {"operation": "generate", "run_override": {"tts": {"service": "openai"}}}
        )
        self._mark_running(main["id"])

        edited = self._patch_segment(
            first["id"], first["revision"], {"optimized_text": "One spoken"}
        )
        # Mirror the store's atomic adoption: the regenerate request uses
        # only server-returned values (new ID + new plan revision), exactly
        # as the UI state holds them after adopting the mutation response.
        # No hand-mixed stale/current combination is constructed here.
        regen = self._start_run(
            {
                "operation": "regenerate",
                "segment_ids": [edited["id"]],
                "speech_plan_revision_id": edited["plan_revision_id"],
            }
        )
        with self.database.session() as session:
            run = session.get(GenerationRun, regen["id"])
            self.assertEqual(edited["plan_revision_id"], run.plan_revision_id)
            main_run = session.get(GenerationRun, main["id"])
            self.assertEqual("pausing", main_run.status)
            self.assertTrue(run.resume_source_on_completion)

        result = self._run_worker(regen["id"])
        # "partial" is correct: only the targeted segment was regenerated,
        # the never-generated sibling "Two" remains outstanding (the live
        # main run was still synthesizing it when interrupted).
        self.assertEqual("partial", result["status"])
        # The newly saved spoken payload is what gets synthesized ...
        self.assertEqual(["One spoken"], self._synthesized_texts(regen["id"]))
        # ... and success leaves no spurious stale behind.
        page_done = self._segments()
        statuses = {
            item["id"]: item["status"]
            for item in page_done["items"]
            if item["id"] == edited["id"]
        }
        self.assertEqual({edited["id"]: "completed"}, statuses)

    def test_genuine_concurrent_revision_still_rejected(self):
        """Segments that are NOT on the active revision must still 409."""
        page = self._segments()
        revision_r1 = page["plan_revision_id"]
        first = page["items"][0]

        main = self._start_run(
            {"operation": "generate", "run_override": {"tts": {"service": "openai"}}}
        )
        self._mark_running(main["id"])

        edited = self._patch_segment(
            first["id"], first["revision"], {"optimized_text": "One spoken"}
        )
        page_r2 = self._segments()
        revision_r2 = page_r2["plan_revision_id"]
        # Mark R2 as used with a queued regen, then a second client edits
        # again: R2 rows are now genuinely stale (R3 active).
        self._start_run(
            {
                "operation": "regenerate",
                "segment_ids": [edited["id"]],
                "speech_plan_revision_id": revision_r2,
            }
        )
        live_r2 = self._segments()
        current_r2 = next(
            item for item in live_r2["items"] if item["id"] == edited["id"]
        )
        self._patch_segment(
            current_r2["id"], current_r2["revision"], {"optimized_text": "One respoken"}
        )
        revision_r3 = self._segments()["plan_revision_id"]
        self.assertNotEqual(revision_r2, revision_r3)

        rejected = self._start_run(
            {
                "operation": "regenerate",
                "segment_ids": [edited["id"]],
                "speech_plan_revision_id": revision_r2,
            },
            expected=409,
        )
        self.assertIn("plan", rejected["error"]["message"].lower())

    def test_tts_and_cue_fields_stay_independent(self):
        """Bulk replace touches only the selected field per segment."""
        page = self._segments()
        first, second = page["items"][0], page["items"][1]
        # Seed a spoken override on the first segment.
        seeded = self._patch_segment(
            first["id"], first["revision"], {"optimized_text": "One spoken"}
        )
        live = self._segments()
        first_live = next(i for i in live["items"] if i["id"] == seeded["id"])
        second_live = next(i for i in live["items"] if i["ordinal"] == second["ordinal"])

        response = self.client.patch(
            f"/api/v1/sessions/{self.session_id}/generation-segments",
            json={
                "updates": [
                    {
                        "id": first_live["id"],
                        "revision": first_live["revision"],
                        "changes": {"text": "One edited"},
                    },
                    {
                        "id": second_live["id"],
                        "revision": second_live["revision"],
                        "changes": {"optimized_text": "Two spoken"},
                    },
                ]
            },
            headers=self.headers,
        )
        self.assertEqual(200, response.status_code, response.get_json())
        items = {item["id"]: item for item in response.get_json()["items"]}
        # A display-text edit clears its own spoken override; the cue-only
        # segment keeps its display text while gaining a spoken override.
        self.assertEqual("One edited", items[first_live["id"]]["text"])
        self.assertIsNone(items[first_live["id"]]["optimized_text"])
        self.assertEqual("Two", items[second_live["id"]]["text"])
        self.assertEqual("Two spoken", items[second_live["id"]]["optimized_text"])
        # Clearing back to null falls back to the cue text (same as empty).
        cleared = self._patch_segment(
            second_live["id"],
            items[second_live["id"]]["revision"],
            {"optimized_text": None},
        )
        self.assertIsNone(cleared["optimized_text"])
        self.assertEqual("Two", cleared["text"])
        # Stale revision guards still hold on the single-segment route.
        self._patch_segment(
            second_live["id"],
            second_live["revision"],
            {"optimized_text": "stale write"},
            expected=409,
        )


    def test_combined_patch_honors_explicit_optimized_text(self):
        """Voiceover cue-only replacement preserves an existing TTS override."""
        page = self._segments()
        first = page["items"][0]
        seeded = self._patch_segment(
            first["id"], first["revision"], {"optimized_text": "One spoken"}
        )
        live = self._segments()
        current = next(i for i in live["items"] if i["id"] == seeded["id"])
        # Combined display + explicit spoken: the explicit override wins over
        # the legacy text-change clear.
        combined = self._patch_segment(
            current["id"],
            current["revision"],
            {"text": "One edited cue", "optimized_text": "One spoken"},
        )
        self.assertEqual("One edited cue", combined["text"])
        self.assertEqual("One spoken", combined["optimized_text"])
        self.assertTrue(combined["optimization_reviewed"])
        self.assertEqual("reviewed", combined["optimization_status"])
        self.assertEqual(
            "One spoken", (combined.get("speech_plan") or {}).get("compiled_text")
        )

    def test_text_only_patch_keeps_legacy_clear(self):
        """Audiobook text-only PATCH still clears the override (legacy)."""
        page = self._segments()
        first = page["items"][0]
        seeded = self._patch_segment(
            first["id"], first["revision"], {"optimized_text": "One spoken"}
        )
        live = self._segments()
        current = next(i for i in live["items"] if i["id"] == seeded["id"])
        cleared = self._patch_segment(
            current["id"], current["revision"], {"text": "One edited"}
        )
        self.assertEqual("One edited", cleared["text"])
        self.assertIsNone(cleared["optimized_text"])
        self.assertEqual("stale", cleared["optimization_status"])
        self.assertEqual("stale", cleared["status"])

    def test_effective_speech_decides_audio_stale(self):
        """Same effective speech retains takes; altered speech stales."""
        page = self._segments()
        first = page["items"][0]
        run = self._start_run(
            {"operation": "generate", "run_override": {"tts": {"service": "openai"}}}
        )
        result = self._run_worker(run["id"], operation="generate")
        self.assertEqual("completed", result["status"])
        live = self._segments()
        current = next(i for i in live["items"] if i["ordinal"] == first["ordinal"])
        self.assertEqual("completed", current["status"])

        # Cue-only edit preserving the fallback speech: display changes, but
        # the effective spoken text ("One") is unchanged, so audio stays.
        retained = self._patch_segment(
            current["id"],
            current["revision"],
            {"text": "One edited cue", "optimized_text": current["text"]},
        )
        self.assertEqual("One edited cue", retained["text"])
        self.assertEqual(current["text"], retained["optimized_text"])
        self.assertEqual("completed", retained["status"])
        live_retained = self._segments()
        kept = next(i for i in live_retained["items"] if i["id"] == retained["id"])
        self.assertEqual("completed", kept["status"])
        active_takes = [t for t in kept.get("takes", []) if t.get("is_active")]
        self.assertTrue(active_takes)
        self.assertTrue(all(t["status"] == "completed" for t in active_takes))

        # Altered effective speech stales audio and its takes.
        staled = self._patch_segment(
            retained["id"],
            retained["revision"],
            {"optimized_text": "One respoken"},
        )
        self.assertEqual("stale", staled["status"])
        live_stale = self._segments()
        stale_row = next(i for i in live_stale["items"] if i["id"] == staled["id"])
        self.assertEqual("stale", stale_row["status"])


if __name__ == "__main__":
    unittest.main()
