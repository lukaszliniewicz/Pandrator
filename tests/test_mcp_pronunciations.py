"""MCP contracts for the reusable pronunciation dictionary."""

import asyncio
import inspect
from types import SimpleNamespace
from typing import Annotated, Literal, get_args, get_origin

import pytest
from pydantic import ValidationError

from pandrator_mcp.clients.application_pronunciations import (
    ApplicationPronunciationMethods,
)
from pandrator_mcp.schemas.pronunciations import (
    CreatePronunciationInput,
    DeletePronunciationInput,
    ListPronunciationsInput,
    PronunciationChanges,
    UpdatePronunciationInput,
)
from pandrator_mcp.tools.pronunciations import (
    create_pronunciation,
    delete_pronunciation,
    list_pronunciations,
    register_pronunciation_tools,
    update_pronunciation,
)


class _RequestClient(ApplicationPronunciationMethods):
    def __init__(self):
        self.calls = []

    def _request_json(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return {"items": []}

    def _request_binary_json(self, *args, **kwargs):
        raise AssertionError("Pronunciation tools do not make binary requests.")


def _entry(**updates):
    return {
        "id": "entry-1",
        "scope": "global",
        "session_id": None,
        "session_name": None,
        "source_form": "Imaoka",
        "language": "en",
        "phonetic": "ee-mah-oh-kah",
        "alphabet": "respelling",
        "backend": "*",
        "status": "reviewed",
        "source": "manual",
        "notes": None,
        "metadata": {"private": "omit"},
        "revision": 2,
        "created_at": "2026-10-11T00:00:00+00:00",
        "updated_at": "2026-10-11T00:00:00+00:00",
        "api_key": "omit",
        **updates,
    }


def test_pronunciation_schemas_are_strict_and_preserve_nullable_patch_fields():
    created = CreatePronunciationInput(source_form="Imaoka", phonetic="ee-mah-oh-kah")
    assert created.language == "und"
    assert created.backend == "*"
    assert created.scope == "global"
    assert created.status == "reviewed"
    assert created.alphabet == "respelling"
    assert created.metadata == {}

    with pytest.raises(ValidationError):
        CreatePronunciationInput(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            unexpected="rejected",
        )
    with pytest.raises(ValidationError, match="session_id"):
        CreatePronunciationInput(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            scope="session",
        )
    with pytest.raises(ValidationError, match="cannot include a session_id"):
        CreatePronunciationInput(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            session_id="session-1",
        )
    with pytest.raises(ValidationError):
        CreatePronunciationInput(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            metadata={"value": float("nan")},
        )
    with pytest.raises(ValidationError, match="16 KiB"):
        CreatePronunciationInput(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            metadata={"value": "x" * (16 * 1024)},
        )

    with pytest.raises(ValidationError):
        PronunciationChanges()
    changes = PronunciationChanges(notes=None, session_id=None)
    assert changes.model_fields_set == {"notes", "session_id"}
    assert changes.model_dump(mode="json", exclude_unset=True) == {
        "session_id": None,
        "notes": None,
    }
    with pytest.raises(ValidationError):
        PronunciationChanges(source_form=None)
    session_scope_patch = PronunciationChanges(scope="session")
    assert session_scope_patch.model_dump(mode="json", exclude_unset=True) == {"scope": "session"}
    with pytest.raises(ValidationError, match="session_id"):
        PronunciationChanges(scope="session", session_id=None)
    with pytest.raises(ValidationError, match="cannot include a session_id"):
        PronunciationChanges(scope="global", session_id="session-1")
    with pytest.raises(ValidationError):
        UpdatePronunciationInput(
            entry_id="entry-1",
            expected_revision=0,
            changes=PronunciationChanges(notes="reviewed"),
        )
    with pytest.raises(ValidationError):
        DeletePronunciationInput(entry_id="entry-1", expected_revision=True)


def test_list_schema_bounds_filter_and_page_values():
    assert ListPronunciationsInput().limit == 20
    assert ListPronunciationsInput(limit=100).limit == 100
    for invalid in (0, 101, True, "20"):
        with pytest.raises(ValidationError):
            ListPronunciationsInput(limit=invalid)
    with pytest.raises(ValidationError):
        ListPronunciationsInput(status="unknown")


def test_application_client_routes_fixed_crud_endpoints_and_revision_headers():
    client = _RequestClient()
    client.pronunciation_request("list", {"query": "Imaoka", "limit": 5})
    path, kwargs = client.calls[-1]
    assert path == "/api/v1/pronunciations"
    assert kwargs["parameters"] == {"limit": 5, "q": "Imaoka"}

    client.pronunciation_request(
        "create",
        {
            "source_form": "Imaoka",
            "phonetic": "ee-mah-oh-kah",
            "metadata": {"language_note": "ja"},
        },
    )
    path, kwargs = client.calls[-1]
    assert path == "/api/v1/pronunciations"
    assert kwargs["method"] == "POST"
    assert kwargs["maximum_body_bytes"] == 32 * 1024
    assert "idempotency_key" not in kwargs

    client.pronunciation_request(
        "update",
        {
            "entry_id": "entry/with slash",
            "expected_revision": 3,
            "changes": {"notes": None, "session_id": None},
        },
    )
    path, kwargs = client.calls[-1]
    assert path.endswith("entry%2Fwith%20slash")
    assert kwargs["method"] == "PATCH"
    assert kwargs["body"] == {"notes": None, "session_id": None}
    assert kwargs["if_match_revision"] == 3
    assert "idempotency_key" not in kwargs

    client.pronunciation_request("delete", {"entry_id": "entry-1", "expected_revision": 4})
    path, kwargs = client.calls[-1]
    assert path.endswith("entry-1")
    assert kwargs["method"] == "DELETE"
    assert kwargs["if_match_revision"] == 4
    assert "body" not in kwargs
    assert "idempotency_key" not in kwargs

    with pytest.raises(ValueError, match="Unknown pronunciation action"):
        client.pronunciation_request("arbitrary", {})
    with pytest.raises(ValueError, match="unsupported fields"):
        client.pronunciation_request("list", {"path": "/arbitrary"})
    with pytest.raises(ValueError, match="unsupported fields"):
        client.pronunciation_request(
            "update",
            {
                "entry_id": "entry-1",
                "expected_revision": 3,
                "changes": {"unrecognized": True},
            },
        )

    client.pronunciation_request(
        "update",
        {
            "entry_id": "entry-1",
            "expected_revision": 5,
            "changes": {"scope": "session"},
        },
    )
    assert client.calls[-1][1]["body"] == {"scope": "session"}
    with pytest.raises(ValueError, match="session_id"):
        client.pronunciation_request(
            "update",
            {
                "entry_id": "entry-1",
                "expected_revision": 5,
                "changes": {"scope": "session", "session_id": None},
            },
        )
    with pytest.raises(ValueError, match="cannot include a session_id"):
        client.pronunciation_request(
            "update",
            {
                "entry_id": "entry-1",
                "expected_revision": 5,
                "changes": {"scope": "global", "session_id": "session-1"},
            },
        )


def test_application_client_rejects_non_finite_and_oversized_json():
    client = _RequestClient()
    with pytest.raises(ValueError, match="finite JSON"):
        client.pronunciation_request(
            "create",
            {
                "source_form": "Imaoka",
                "phonetic": "ee-mah-oh-kah",
                "metadata": {"value": float("inf")},
            },
        )
    with pytest.raises(ValueError, match="16 KiB"):
        client.pronunciation_request(
            "create",
            {
                "source_form": "Imaoka",
                "phonetic": "ee-mah-oh-kah",
                "metadata": {"value": "x" * (16 * 1024)},
            },
        )
    with pytest.raises(ValueError, match="32 KiB"):
        client.pronunciation_request(
            "create",
            {
                "source_form": "Imaoka",
                "phonetic": "ee-mah-oh-kah",
                "notes": "x" * (32 * 1024),
            },
        )
    assert client.calls == []


def test_tools_project_safe_results_and_report_only_static_speech_effects():
    class Application:
        def __init__(self):
            self.calls = []

        def pronunciation_request(self, action, arguments):
            self.calls.append((action, arguments))
            if action == "list":
                return {"items": [_entry()]}
            if action == "delete":
                return {}
            return _entry()

    application = Application()
    runtime = SimpleNamespace(require_application=lambda: application)
    listed = list_pronunciations(runtime, ListPronunciationsInput(query="Imaoka"))
    assert listed.result == {
        "items": [
            {key: value for key, value in _entry().items() if key not in {"metadata", "api_key"}}
        ]
    }

    created = create_pronunciation(
        runtime,
        CreatePronunciationInput(source_form="Imaoka", phonetic="ee-mah-oh-kah"),
    )
    effects = created.result["effects"]
    assert effects == {
        "existing_recordings_modified": False,
        "manual_speech_overrides_preserved": True,
        "requires_speech_preparation_for_existing_audio": True,
    }
    assert "metadata" not in created.result

    updated = update_pronunciation(
        runtime,
        UpdatePronunciationInput(
            entry_id="entry-1",
            expected_revision=2,
            changes=PronunciationChanges(notes=None, session_id=None),
        ),
    )
    assert application.calls[-1] == (
        "update",
        {
            "entry_id": "entry-1",
            "expected_revision": 2,
            "changes": {"session_id": None, "notes": None},
        },
    )
    assert updated.result["effects"] == effects

    deleted = delete_pronunciation(
        runtime, DeletePronunciationInput(entry_id="entry-1", expected_revision=3)
    )
    assert deleted.result == {"id": "entry-1", "deleted": True, "effects": effects}


def test_registration_exposes_typed_signatures_annotations_and_response_mode():
    registered = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def capture(function):
                registered[name] = (function, title, annotations)
                return function

            return capture

    class Application:
        def pronunciation_request(self, action, arguments):
            return {"items": []}

    runtime = SimpleNamespace(require_application=lambda: Application())
    observed_modes = []

    def validated_call(handler, call_runtime, model, values):
        return {"result": handler(call_runtime, model.model_validate(values)).result}

    def response(envelope, mode):
        observed_modes.append(mode)
        return {"mode": mode, **envelope}

    register_pronunciation_tools(
        Server(),
        runtime,
        validated_call,
        response,
        read_only="read",
        revisioned_write_action="revisioned-write",
        destructive_action="destructive",
    )
    assert set(registered) == {
        "pandrator_list_pronunciations",
        "pandrator_create_pronunciation",
        "pandrator_update_pronunciation",
        "pandrator_delete_pronunciation",
    }
    expected = {
        "pandrator_list_pronunciations": (
            "List pronunciation entries",
            "read",
            "Find reusable respellings by text, language, scope and review status.",
        ),
        "pandrator_create_pronunciation": (
            "Create pronunciation entry",
            "revisioned-write",
            "Edit the reusable pronunciation dictionary. Existing audio and manual speech overrides stay unchanged; re-run speech preparation, then refresh changed audio to apply it to existing recordings.",
        ),
        "pandrator_update_pronunciation": (
            "Update pronunciation entry",
            "revisioned-write",
            "Edit the reusable pronunciation dictionary. Existing audio and manual speech overrides stay unchanged; re-run speech preparation, then refresh changed audio to apply it to existing recordings.",
        ),
        "pandrator_delete_pronunciation": (
            "Delete pronunciation entry",
            "destructive",
            "Edit the reusable pronunciation dictionary. Existing audio and manual speech overrides stay unchanged; re-run speech preparation, then refresh changed audio to apply it to existing recordings.",
        ),
    }
    for name, (function, title, annotations) in registered.items():
        expected_title, expected_annotations, expected_docstring = expected[name]
        assert (title, annotations) == (expected_title, expected_annotations)
        assert function.__doc__ == expected_docstring
        signature = inspect.signature(function)
        assert "values" not in signature.parameters
        assert signature.parameters["response_mode"].default == "standard"
        assert get_origin(signature.parameters["response_mode"].annotation) is Literal

    create_tool = registered["pandrator_create_pronunciation"][0]
    session_id_annotation = inspect.signature(create_tool).parameters["session_id"].annotation
    assert get_origin(session_id_annotation) is Annotated
    assert get_args(session_id_annotation)[0] is str
    result = create_tool(
        source_form="Imaoka",
        phonetic="ee-mah-oh-kah",
        response_mode="structured",
    )
    assert result["mode"] == "structured"
    assert observed_modes == ["structured"]
    with pytest.raises(ValueError, match="response_mode"):
        create_tool(
            source_form="Imaoka",
            phonetic="ee-mah-oh-kah",
            response_mode="unexpected",
        )


def test_tools_expose_typed_mcp_schemas_and_structured_calls_when_sdk_is_available():
    try:
        from mcp import Client
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError:
        pytest.skip("MCP SDK is not installed")

    class Application:
        def pronunciation_request(self, action, arguments):
            return {"items": [_entry()]}

    runtime = SimpleNamespace(require_application=lambda: Application())

    def validated_call(handler, call_runtime, model, values):
        return {"result": handler(call_runtime, model.model_validate(values)).result}

    def response(envelope, mode):
        return {"mode": mode, **envelope}

    server = MCPServer("pronunciation-contract")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    )
    destructive = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=False,
    )
    register_pronunciation_tools(
        server,
        runtime,
        validated_call,
        response,
        read_only=read_only,
        revisioned_write_action=write,
        destructive_action=destructive,
    )

    async def exercise():
        async with Client(server, mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            list_tool = tools["pandrator_list_pronunciations"]
            assert list_tool.title == "List pronunciation entries"
            assert list_tool.annotations.read_only_hint is True
            assert list_tool.input_schema["properties"]["limit"]["default"] == 20
            result = await client.call_tool(
                "pandrator_list_pronunciations",
                {"query": "Imaoka", "response_mode": "structured"},
            )
            assert not result.is_error
            assert result.structured_content["mode"] == "structured"
            assert result.structured_content["result"]["items"][0]["id"] == "entry-1"

            update_tool = tools["pandrator_update_pronunciation"]
            assert update_tool.annotations.idempotent_hint is False
            assert "changes" in update_tool.input_schema["properties"]
            assert PronunciationChanges.model_json_schema()["additionalProperties"] is False
            result = await client.call_tool(
                "pandrator_update_pronunciation",
                {
                    "entry_id": "entry-1",
                    "expected_revision": 2,
                    "changes": {"notes": None},
                    "response_mode": "structured",
                },
            )
            assert not result.is_error

    asyncio.run(exercise())
