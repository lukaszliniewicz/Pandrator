import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.schemas.subtitle_evidence import (
    GetSubtitleEvidenceInput,
    RequestSubtitleEvidenceInput,
    ResolveSubtitleEvidenceInput,
)
from pandrator_mcp.tools.subtitle_evidence import (
    get_subtitle_evidence,
    request_subtitle_evidence,
    resolve_subtitle_evidence,
)


class _Application:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.status = "queued"

    def request_subtitle_evidence(self, session_id, **kwargs):
        self.calls.append(("request", {"session_id": session_id, **kwargs}))
        return {
            "record": {
                "id": "evidence-1",
                "session_id": session_id,
                "source_artifact_id": kwargs["source_artifact_id"],
                "cue_id": kwargs["cue_id"],
                "status": self.status,
                "reason": kwargs["reason"],
                "audio_model_ids": kwargs["audio_model_ids"],
                "private_path": "/must/not/leak.wav",
            },
            "job": {"id": "job-1", "status": self.status},
            "unrelated": "must not leak",
        }

    def get_subtitle_evidence(self, evidence_id):
        self.calls.append(("get", {"evidence_id": evidence_id}))
        return {
            "record": {
                "id": evidence_id,
                "status": self.status,
                "candidates": [],
                "private_path": "/must/not/leak.json",
            },
            "job": {"id": "job-1", "status": self.status},
        }

    def resolve_subtitle_evidence(self, session_id, evidence_id, **kwargs):
        self.calls.append(
            (
                "resolve",
                {
                    "session_id": session_id,
                    "evidence_id": evidence_id,
                    **kwargs,
                },
            )
        )
        return {
            "record": {
                "id": evidence_id,
                "session_id": session_id,
                "status": "resolved",
                "resolution": {"action": kwargs["action"]},
            },
            "job": {"id": "job-1", "status": "succeeded"},
        }


class SubtitleEvidenceMcpTests(unittest.TestCase):
    def setUp(self):
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_inputs_are_strict_and_resolution_shapes_are_action_specific(self):
        with self.assertRaises(ValidationError):
            RequestSubtitleEvidenceInput(
                session_id="session-1",
                source_artifact_id="artifact-1",
                cue_id=1,
                reason="check",
                routes=["whisper", "whisper"],
                idempotency_key="evidence:one",
            )
        with self.assertRaises(ValidationError):
            ResolveSubtitleEvidenceInput(
                session_id="session-1",
                evidence_id="evidence-1",
                action="accepted",
                idempotency_key="resolve:one",
            )
        with self.assertRaises(ValidationError):
            ResolveSubtitleEvidenceInput(
                session_id="session-1",
                evidence_id="evidence-1",
                action="accepted",
                candidate_id="whisper-1",
                text="not valid for accepted evidence",
                idempotency_key="resolve:two",
            )
        with self.assertRaises(ValidationError):
            ResolveSubtitleEvidenceInput(
                session_id="session-1",
                evidence_id="evidence-1",
                action="uncertain",
                idempotency_key="resolve:three",
            )
        with self.assertRaises(ValidationError):
            GetSubtitleEvidenceInput(evidence_id="evidence-1", extra="rejected")

    def test_request_projects_the_record_envelope_and_points_to_polling(self):
        outcome = request_subtitle_evidence(
            self.runtime,
            RequestSubtitleEvidenceInput(
                session_id="session-1",
                source_artifact_id="artifact-1",
                cue_id=5,
                reason="The cue is incoherent in context.",
                routes=["whisper", "moss"],
                idempotency_key="evidence:request:one",
            ),
        )
        self.assertEqual("evidence-1", outcome.result["evidence_id"])
        self.assertEqual("job-1", outcome.result["job_id"])
        self.assertEqual("queued", outcome.result["job_status"])
        self.assertNotIn("private_path", outcome.result)
        self.assertNotIn("unrelated", outcome.result)
        self.assertEqual(
            "pandrator_get_subtitle_evidence", outcome.next_actions[0].tool
        )
        self.assertEqual(
            ["whisper", "moss"], self.application.calls[0][1]["routes"]
        )
        self.assertEqual([], outcome.result["audio_model_ids"])

    def test_get_stops_polling_at_terminal_state_and_resolve_is_explicit(self):
        queued = get_subtitle_evidence(
            self.runtime, GetSubtitleEvidenceInput(evidence_id="evidence-1")
        )
        self.assertEqual(1, len(queued.next_actions))
        self.application.status = "completed"
        completed = get_subtitle_evidence(
            self.runtime, GetSubtitleEvidenceInput(evidence_id="evidence-1")
        )
        self.assertEqual([], completed.next_actions)

        resolved = resolve_subtitle_evidence(
            self.runtime,
            ResolveSubtitleEvidenceInput(
                session_id="session-1",
                evidence_id="evidence-1",
                action="accepted",
                candidate_id="whisper-1",
                note="Matches the surrounding sentence.",
                idempotency_key="evidence:resolve:one",
            ),
        )
        self.assertEqual("resolved", resolved["status"])
        call = self.application.calls[-1][1]
        self.assertEqual("accepted", call["action"])
        self.assertEqual("whisper-1", call["candidate_id"])


if __name__ == "__main__":
    unittest.main()
