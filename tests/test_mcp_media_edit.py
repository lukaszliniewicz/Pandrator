import inspect
import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.schemas.media_edit import (
    GetMediaEditArguments,
    InspectMediaEditBoundaryArguments,
    ListMediaEditCutsArguments,
    PrepareMediaEditArguments,
    ProposeMediaEditArguments,
    RefineMediaEditBoundaryArguments,
    RenderMediaEditArguments,
    UpdateMediaEditArguments,
)
from pandrator_mcp.schemas.sessions import (
    CreateSessionInput,
    ListSessionsInput,
    UpdateSessionInput,
)
from pandrator_mcp.schemas.workflow import DescribeParametersInput
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.media_edit import (
    get_media_edit,
    inspect_media_edit_boundary,
    list_media_edit_cuts,
    prepare_media_edit,
    propose_media_edit,
    refine_media_edit_boundary,
    render_media_edit,
    update_media_edit,
)
from tests.test_mcp_application_client import FakeResponse, FakeSession, local_registry


class _Application:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def get_media_edit(self, session_id):
        self.calls.append(("get", session_id))
        return {"session_id": session_id, "plan": None}

    def list_media_edit_cuts(self, session_id, *, revision=None):
        self.calls.append(("list_cuts", (session_id, revision)))
        return {"session_id": session_id, "revision": revision or 3, "cuts": []}

    def inspect_media_edit_boundary(self, session_id, **kwargs):
        self.calls.append(("inspect_boundary", (session_id, kwargs)))
        return {
            "session_id": session_id,
            "revision": kwargs.get("revision") or 3,
            "cut_index": kwargs["cut_index"],
            "edge": kwargs["edge"],
        }

    def refine_media_edit_boundary(self, session_id, **kwargs):
        self.calls.append(("refine_boundary", (session_id, kwargs)))
        return {
            "session_id": session_id,
            "current_revision": {"revision": 4},
            "affected_cut": {"index": kwargs["cut_index"]},
        }

    def prepare_media_edit(self, session_id, *, force=False, idempotency_key):
        self.calls.append(("prepare", (session_id, force, idempotency_key)))
        return {"session_id": session_id, "plan": {"revision": 1}}

    def update_media_edit(self, session_id, **kwargs):
        self.calls.append(("update", (session_id, kwargs)))
        return {"session_id": session_id, "plan": {"revision": 2}}

    def propose_media_edit(
        self, session_id, *, revision, instructions, model=None, idempotency_key
    ):
        self.calls.append(
            ("propose", (session_id, revision, instructions, model, idempotency_key))
        )
        return {"id": "job-propose", "status": "queued", "progress": 0.0}

    def render_media_edit(self, session_id, *, revision, idempotency_key, subtitles_only=False):
        self.calls.append(("render", (session_id, revision, idempotency_key)))
        self.subtitles_only = subtitles_only
        return {"id": "job-render", "status": "queued", "progress": 0.0}

    def wait_for_job(self, work_id, *, timeout_seconds):
        self.calls.append(("wait", (work_id, timeout_seconds)))
        return {
            "id": work_id,
            "status": "succeeded",
            "progress": 1.0,
            "result": {"artifact_id": "artifact-1"},
        }


class MediaEditSchemaTests(unittest.TestCase):
    def test_media_edit_workflow_kind_is_allowed_in_session_and_parameter_filters(self):
        self.assertEqual(
            "media_edit", ListSessionsInput(workflow_kind="media_edit").workflow_kind
        )
        self.assertEqual(
            "media_edit",
            CreateSessionInput(
                name="Edit",
                workflow_kind="media_edit",
                idempotency_key="session:edit:1",
            ).workflow_kind,
        )
        self.assertEqual(
            "media_edit",
            UpdateSessionInput(
                session_id="session-1",
                expected_revision=1,
                workflow_kind="media_edit",
                name="Edit",
                idempotency_key="session:update:1",
            ).workflow_kind,
        )
        self.assertEqual(
            "media_edit",
            DescribeParametersInput(workflow_kind="media_edit").workflow_kind,
        )

    def test_media_edit_arguments_are_strict_and_bound(self):
        valid = UpdateMediaEditArguments(
            session_id="session-1",
            expected_revision=1,
            keep_ranges=[{"start_ms": 0, "end_ms": 1000}],
            idempotency_key="media:update:1",
        )
        self.assertEqual(1, valid.expected_revision)
        self.assertEqual(
            "trim the dead air",
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=1,
                instructions="  trim the dead air  ",
                idempotency_key="media:propose:1",
            ).instructions,
        )
        self.assertEqual(
            "custom/provider-model",
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=1,
                instructions="trim the dead air",
                model="  custom/provider-model  ",
                idempotency_key="media:propose:model",
            ).model,
        )
        self.assertTrue(
            RenderMediaEditArguments(
                session_id="session-1",
                revision=1,
                idempotency_key="media:render:1",
            ).wait
        )
        self.assertEqual(
            False,
            PrepareMediaEditArguments(
                session_id="session-1",
                idempotency_key="media:prepare:1",
            ).force,
        )

        invalid_values = (
            {
                "session_id": "session-1",
                "expected_revision": 0,
                "keep_ranges": [{"start_ms": 0, "end_ms": 1}],
            },
            {"session_id": "session-1", "expected_revision": 1, "keep_ranges": []},
            {
                "session_id": "session-1",
                "expected_revision": 1,
                "keep_ranges": [{"start_ms": -1, "end_ms": 1}],
            },
            {
                "session_id": "session-1",
                "expected_revision": 1,
                "keep_ranges": [{"start_ms": 0, "end_ms": 0}],
            },
            {
                "session_id": "session-1",
                "expected_revision": 1,
                "keep_ranges": [{"start_ms": 0, "end_ms": 1, "unknown": True}],
            },
            {
                "session_id": "session-1",
                "expected_revision": 1,
                "keep_ranges": [{"start_ms": 0, "end_ms": 1}],
                "unknown": True,
            },
        )
        for values in invalid_values:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                UpdateMediaEditArguments.model_validate(
                    {**values, "idempotency_key": "media:update:invalid"}
                )
        with self.assertRaises(ValidationError):
            UpdateMediaEditArguments(
                session_id="session-1",
                expected_revision=1,
                keep_ranges=[{"start_ms": 10, "end_ms": 5}],
                idempotency_key="media:update:range",
            )
        with self.assertRaises(ValidationError):
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=0,
                instructions="x",
                idempotency_key="media:propose:bad",
            )
        with self.assertRaises(ValidationError):
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=1,
                instructions="   ",
                idempotency_key="media:propose:blank",
            )
        with self.assertRaises(ValidationError):
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=1,
                instructions="trim",
                model="   ",
                idempotency_key="media:propose:model-blank",
            )
        with self.assertRaises(ValidationError):
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=1,
                instructions="trim",
                model="m" * 513,
                idempotency_key="media:propose:model-long",
            )
        with self.assertRaises(ValidationError):
            RenderMediaEditArguments(
                session_id="session-1",
                revision=1,
                timeout_seconds=3_601,
                idempotency_key="media:render:bad",
            )
        with self.assertRaises(ValidationError):
            GetMediaEditArguments(session_id="session-1", extra=True)

    def test_boundary_arguments_require_one_bounded_target(self):
        inspection = InspectMediaEditBoundaryArguments(
            session_id="session-1",
            cut_index=2,
            edge="end",
            context_ms=750,
            cue_limit=3,
        )
        self.assertEqual(750, inspection.context_ms)
        self.assertEqual(
            2,
            ListMediaEditCutsArguments(session_id="session-1", revision=2).revision,
        )
        refinement = RefineMediaEditBoundaryArguments(
            session_id="session-1",
            expected_revision=2,
            cut_index=1,
            edge="start",
            delta_ms=-100,
            idempotency_key="media:boundary:1",
        )
        self.assertEqual(-100, refinement.delta_ms)
        for values in (
            {},
            {"position_ms": 900, "delta_ms": -100},
            {"delta_ms": 0},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                RefineMediaEditBoundaryArguments(
                    session_id="session-1",
                    expected_revision=2,
                    cut_index=1,
                    edge="start",
                    idempotency_key="media:boundary:bad",
                    **values,
                )
        with self.assertRaises(ValidationError):
            InspectMediaEditBoundaryArguments(
                session_id="session-1",
                cut_index=1,
                edge="start",
                context_ms=30_001,
            )


class MediaEditClientTests(unittest.TestCase):
    def test_client_media_edit_methods_use_exact_routes_bodies_and_revision_header(
        self,
    ):
        session = FakeSession(
            [
                FakeResponse(200, {"session_id": "session-1"}),
                FakeResponse(200, {"session_id": "session-1", "plan": {"revision": 1}}),
                FakeResponse(200, {"session_id": "session-1", "plan": {"revision": 2}}),
                FakeResponse(202, {"id": "job-propose", "status": "queued"}),
                FakeResponse(202, {"id": "job-render", "status": "queued"}),
                FakeResponse(202, {"id": "job-subtitles", "status": "queued"}),
            ]
        )
        client = ApplicationClient(
            local_registry("http://127.0.0.1:8097").bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        client.get_media_edit("session-1")
        client.prepare_media_edit(
            "session-1", force=True, idempotency_key="media:prepare:1"
        )
        client.update_media_edit(
            "session-1",
            expected_revision=1,
            idempotency_key="media:update:1",
            keep_ranges=[{"start_ms": 0, "end_ms": 1000}],
            instructions="Keep the intro.",
            reviewed=True,
        )
        client.propose_media_edit(
            "session-1",
            revision=2,
            instructions="Remove pauses.",
            model="custom/provider-model",
            idempotency_key="media:propose:1",
        )
        client.render_media_edit(
            "session-1", revision=2, idempotency_key="media:render:1"
        )

        calls = session.calls
        self.assertEqual(
            "/api/v1/sessions/session-1/media-edit",
            calls[0]["url"].split("http://127.0.0.1:8097", 1)[1],
        )
        self.assertEqual(
            "/api/v1/sessions/session-1/media-edit/prepare",
            calls[1]["url"].split("http://127.0.0.1:8097", 1)[1],
        )
        self.assertEqual("POST", calls[1]["method"])
        self.assertEqual("media:prepare:1", calls[1]["headers"]["Idempotency-Key"])
        self.assertEqual({"force": True}, json.loads(calls[1]["data"]))
        self.assertEqual("PUT", calls[2]["method"])
        self.assertEqual('"1"', calls[2]["headers"]["If-Match"])
        self.assertEqual("media:update:1", calls[2]["headers"]["Idempotency-Key"])
        self.assertEqual(
            {
                "keep_ranges": [{"start_ms": 0, "end_ms": 1000}],
                "instructions": "Keep the intro.",
                "reviewed": True,
            },
            json.loads(calls[2]["data"]),
        )
        self.assertTrue(calls[3]["url"].endswith("/media-edit/propose"))
        self.assertEqual("media:propose:1", calls[3]["headers"]["Idempotency-Key"])
        self.assertEqual(
            {
                "revision": 2,
                "instructions": "Remove pauses.",
                "model": "custom/provider-model",
            },
            json.loads(calls[3]["data"]),
        )
        self.assertTrue(calls[4]["url"].endswith("/media-edit/render"))
        self.assertEqual("media:render:1", calls[4]["headers"]["Idempotency-Key"])
        self.assertEqual({"revision": 2}, json.loads(calls[4]["data"]))
        client.render_media_edit(
            "session-1", revision=2, idempotency_key="media:subtitles:1", subtitles_only=True
        )
        self.assertEqual({"revision": 2, "subtitles_only": True}, json.loads(session.calls[-1]["data"]))

    def test_client_boundary_methods_use_bounded_query_and_atomic_patch(self):
        session = FakeSession(
            [
                FakeResponse(200, {"session_id": "session-1", "cuts": []}),
                FakeResponse(200, {"session_id": "session-1", "cut_index": 2}),
                FakeResponse(
                    200,
                    {
                        "session_id": "session-1",
                        "current_revision": {"revision": 4},
                    },
                ),
            ]
        )
        client = ApplicationClient(
            local_registry("http://127.0.0.1:8097").bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        client.list_media_edit_cuts("session-1", revision=3)
        client.inspect_media_edit_boundary(
            "session-1",
            revision=3,
            cut_index=2,
            edge="end",
            context_ms=750,
            cue_limit=4,
        )
        client.refine_media_edit_boundary(
            "session-1",
            expected_revision=3,
            cut_index=2,
            edge="end",
            delta_ms=-100,
            idempotency_key="media:boundary:2",
        )

        calls = session.calls
        self.assertEqual({"revision": 3}, calls[0]["params"])
        self.assertEqual(
            {
                "revision": 3,
                "cut_index": 2,
                "edge": "end",
                "context_ms": 750,
                "cue_limit": 4,
            },
            calls[1]["params"],
        )
        self.assertEqual("PATCH", calls[2]["method"])
        self.assertEqual('"3"', calls[2]["headers"]["If-Match"])
        self.assertEqual("media:boundary:2", calls[2]["headers"]["Idempotency-Key"])
        self.assertEqual(
            {"cut_index": 2, "edge": "end", "delta_ms": -100},
            json.loads(calls[2]["data"]),
        )


class MediaEditToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_state_and_prepare_update_paths(self):
        self.assertEqual(
            {"session_id": "session-1", "plan": None},
            get_media_edit(self.runtime, GetMediaEditArguments(session_id="session-1")),
        )
        prepare_media_edit(
            self.runtime,
            PrepareMediaEditArguments(
                session_id="session-1",
                force=True,
                idempotency_key="media:prepare:1",
            ),
        )
        update_media_edit(
            self.runtime,
            UpdateMediaEditArguments(
                session_id="session-1",
                expected_revision=1,
                keep_ranges=[{"start_ms": 0, "end_ms": 1000, "label": "intro"}],
                reviewed=True,
                idempotency_key="media:update:1",
            ),
        )
        self.assertEqual(
            ("prepare", ("session-1", True, "media:prepare:1")),
            self.application.calls[1],
        )
        name, (session_id, payload) = self.application.calls[2]
        self.assertEqual("update", name)
        self.assertEqual("session-1", session_id)
        self.assertEqual(1, payload["expected_revision"])
        self.assertEqual(
            [{"start_ms": 0, "end_ms": 1000, "label": "intro"}],
            payload["keep_ranges"],
        )

    def test_propose_and_render_wait_false_vs_true(self):
        queued = propose_media_edit(
            self.runtime,
            ProposeMediaEditArguments(
                session_id="session-1",
                revision=1,
                instructions="Remove pauses.",
                model="custom/provider-model",
                wait=False,
                idempotency_key="media:propose:1",
            ),
        )
        self.assertEqual("job-propose", queued.work.id)
        self.assertEqual("queued", queued.work.state)
        self.assertEqual(
            (
                "propose",
                (
                    "session-1",
                    1,
                    "Remove pauses.",
                    "custom/provider-model",
                    "media:propose:1",
                ),
            ),
            self.application.calls[0],
        )
        self.assertFalse(any(call[0] == "wait" for call in self.application.calls))

        completed = render_media_edit(
            self.runtime,
            RenderMediaEditArguments(
                session_id="session-1",
                revision=1,
                wait=True,
                timeout_seconds=17,
                idempotency_key="media:render:1",
            ),
        )
        self.assertEqual("job-render", completed.work.id)
        self.assertEqual("succeeded", completed.work.state)
        self.assertEqual(("wait", ("job-render", 17)), self.application.calls[-1])
        self.assertEqual({"artifact_id": "artifact-1"}, completed.result["result"])
        render_media_edit(self.runtime, RenderMediaEditArguments(
            session_id="session-1", revision=1, wait=False,
            subtitles_only=True, idempotency_key="media:subtitles:2",
        ))
        self.assertTrue(self.application.subtitles_only)

    def test_boundary_tools_return_concise_reinspection_actions(self):
        listed = list_media_edit_cuts(
            self.runtime,
            ListMediaEditCutsArguments(session_id="session-1", revision=3),
        )
        inspected = inspect_media_edit_boundary(
            self.runtime,
            InspectMediaEditBoundaryArguments(
                session_id="session-1",
                revision=3,
                cut_index=2,
                edge="end",
                context_ms=750,
                cue_limit=4,
            ),
        )
        refined = refine_media_edit_boundary(
            self.runtime,
            RefineMediaEditBoundaryArguments(
                session_id="session-1",
                expected_revision=3,
                cut_index=2,
                edge="end",
                delta_ms=-100,
                idempotency_key="media:boundary:3",
            ),
        )

        self.assertEqual(3, listed["revision"])
        self.assertEqual("end", inspected["edge"])
        self.assertEqual(
            [
                "pandrator_list_media_edit_cuts",
                "pandrator_inspect_media_edit_boundary",
            ],
            [action.tool for action in refined.next_actions],
        )
        self.assertEqual(4, refined.next_actions[0].arguments["revision"])
        self.assertEqual(2, refined.next_actions[1].arguments["cut_index"])


class MediaEditServerRegistrationTests(unittest.TestCase):
    def test_server_source_registers_all_media_edit_tools(self):
        source = inspect.getsource(build_server)
        for name in (
            "pandrator_get_media_edit",
            "pandrator_list_media_edit_cuts",
            "pandrator_inspect_media_edit_boundary",
            "pandrator_prepare_media_edit",
            "pandrator_update_media_edit",
            "pandrator_refine_media_edit_boundary",
            "pandrator_propose_media_edit",
            "pandrator_render_media_edit",
        ):
            self.assertIn(f'name="{name}"', source)


if __name__ == "__main__":
    unittest.main()
