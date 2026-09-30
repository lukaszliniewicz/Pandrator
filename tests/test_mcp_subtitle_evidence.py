import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import get_args
from unittest.mock import patch

from pydantic import ValidationError

from pandrator.web.schemas import SubtitleEvidenceCreateRequest
from pandrator.web.subtitle_evidence import EVIDENCE_ROUTES, evidence_stt_routes
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
    def test_api_mcp_and_service_route_enums_match_all_seven_routes(self):
        expected = set(evidence_stt_routes()) | {"audio_llm"}
        mcp_route_type = get_args(
            RequestSubtitleEvidenceInput.model_fields["routes"].annotation
        )[0]
        web_route_type = get_args(
            SubtitleEvidenceCreateRequest.model_fields["routes"].annotation
        )[0]
        mcp_routes = set(get_args(mcp_route_type))
        web_routes = set(get_args(web_route_type))
        self.assertEqual(expected, mcp_routes)
        self.assertEqual(expected, web_routes)
        self.assertEqual(expected, set(EVIDENCE_ROUTES))

        routes = evidence_stt_routes() + ["audio_llm"]
        web_request = SubtitleEvidenceCreateRequest(
            source_artifact_id="artifact-1",
            cue_id=1,
            reason="Check every supported route.",
            routes=routes,
            audio_model_ids=["audio-model"],
        )
        mcp_request = RequestSubtitleEvidenceInput(
            session_id="session-1",
            source_artifact_id="artifact-1",
            cue_id=1,
            reason="Check every supported route.",
            routes=routes,
            audio_model_ids=["audio-model"],
            idempotency_key="evidence:all-routes",
        )
        self.assertEqual(expected, set(web_request.routes))
        self.assertEqual(expected, set(mcp_request.routes))

    def test_flat_server_tool_registration_matches_route_schemas(self):
        try:
            from pandrator_mcp.context import build_runtime
            from pandrator_mcp.server import build_server
            from pandrator_mcp.settings import McpSettings

            runtime = build_runtime(
                McpSettings(
                    target_name="unconfigured",
                    configuration_path=Path("/tmp/pandrator-missing-targets.json"),
                )
            )
            server = build_server(runtime)
        except (ImportError, RuntimeError) as error:
            self.skipTest(f"MCP server dependency is unavailable: {error}")

        registered = asyncio.run(server.list_tools())
        tool = next(
            item
            for item in registered
            if item.name == "pandrator_request_subtitle_evidence"
        )
        route_schema = tool.input_schema["properties"]["routes"]
        self.assertEqual(
            set(evidence_stt_routes()) | {"audio_llm"},
            set(route_schema["items"]["enum"]),
        )
        self.assertEqual(7, route_schema["maxItems"])

    def test_failed_job_does_not_keep_polling_a_queued_record(self):
        with patch.object(self.application, "get_subtitle_evidence", return_value={
            "record": {"id": "evidence-1", "status": "queued", "error_message": None},
            "job": {"id": "job-1", "status": "failed", "error_message": "Service unavailable"},
        }):
            outcome = get_subtitle_evidence(
                self.runtime, GetSubtitleEvidenceInput(evidence_id="evidence-1")
            )
        self.assertEqual("failed", outcome.result["status"])
        self.assertEqual("Service unavailable", outcome.result["error_message"])
        self.assertEqual([], outcome.next_actions)

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
