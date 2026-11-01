"""Bounded request, schema, and MCP registration coverage for account tools."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast, get_args

from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import _PASSTHROUGH_ERROR_CODES, ApplicationClient
from pandrator_mcp.clients.application_accounts import ApplicationAccountMethods
from pandrator_mcp.context import McpRuntime, build_runtime
from pandrator_mcp.errors import FailureCode, PandratorMcpError
from pandrator_mcp.schemas import (
    TOOL_INPUT_MODELS,
    CreateUserInput,
    IssueUserPasswordResetInput,
    ListUsersInput,
    UpdateUserInput,
)
from pandrator_mcp.schemas.accounts import UserAccountChanges
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.accounts import (
    create_user,
    issue_user_password_reset,
    list_users,
    update_user,
)

try:
    from mcp import Client
except ImportError:  # pragma: no cover - environment-dependent optional SDK
    Client = None


_ACCOUNT = {
    "user_id": "user-1",
    "login_name": "reader",
    "display_name": "Reader",
    "role": "user",
    "is_active": True,
    "auth_revision": 3,
    "password_configured": False,
}
_PUBLIC_ACCOUNT_FIELDS = tuple(_ACCOUNT)
_ACCOUNT_WITH_UNEXPECTED_SECRETS = {
    **_ACCOUNT,
    "password_hash": "must-not-escape",
    "reset_token": "must-not-escape",
    "private_key": "must-not-escape",
}
_ACCOUNT_CODES = (
    "account_not_found",
    "stale_account",
    "login_name_taken",
    "last_administrator",
    "initial_administrator_required",
    "account_disabled",
    "invalid_login_name",
    "invalid_display_name",
    "invalid_role",
    "invalid_account_state",
)


class _RequestRecorder(ApplicationAccountMethods):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response: dict[str, Any] = {}

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"path": path, **kwargs})
        return self.response

    def _request_binary_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Account requests must use the JSON application transport.")


class _FakeBinding:
    def resolve(self) -> SimpleNamespace:
        return SimpleNamespace(application=SimpleNamespace(origin="https://app.example"))


class _FakeAccountApplication:
    binding = _FakeBinding()

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_response: dict[str, Any] = {
            "items": [_ACCOUNT_WITH_UNEXPECTED_SECRETS],
            "initial_user_id": "initial-admin",
            "named_user_access": False,
            "private_metadata": "must-not-escape",
        }

    def account_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, arguments))
        if action == "list":
            return self.list_response
        if action == "issue_reset":
            return {
                "grant_id": "grant-1",
                "user_id": arguments["user_id"],
                "expires_at": "2026-10-23T12:30:00Z",
                "reset_path": "/password-reset#token=" + "A" * 43,
                "token_hash": "must-not-escape",
                "password": "must-not-escape",
            }
        return _ACCOUNT_WITH_UNEXPECTED_SECRETS


def _runtime(application: _FakeAccountApplication) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


class ApplicationAccountRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = _RequestRecorder()

    def test_list_uses_fixed_read_route_and_no_request_body(self) -> None:
        self.client.account_request("list", {})
        self.assertEqual(
            {"path": "/api/v1/admin/users", "read_only_operation": True},
            self.client.calls[0],
        )

    def test_create_defaults_role_and_keeps_idempotency_key_out_of_body(self) -> None:
        self.client.account_request(
            "create",
            {
                "login_name": "reader",
                "display_name": "Reader",
                "idempotency_key": "account-create-1",
            },
        )
        self.assertEqual(
            {
                "path": "/api/v1/admin/users",
                "method": "POST",
                "body": {
                    "login_name": "reader",
                    "display_name": "Reader",
                    "role": "user",
                },
                "idempotency_key": "account-create-1",
                "maximum_body_bytes": 16 * 1024,
            },
            self.client.calls[0],
        )

    def test_update_quotes_user_id_and_preserves_false(self) -> None:
        self.client.account_request(
            "update",
            {
                "user_id": "team/admin?one",
                "expected_auth_revision": 4,
                "changes": {"is_active": False},
                "idempotency_key": "account-update-1",
            },
        )
        call = self.client.calls[0]
        self.assertEqual("/api/v1/admin/users/team%2Fadmin%3Fone", call["path"])
        self.assertEqual("PATCH", call["method"])
        self.assertEqual(
            {"expected_auth_revision": 4, "is_active": False},
            call["body"],
        )
        self.assertEqual("account-update-1", call["idempotency_key"])

    def test_reset_is_non_idempotent_and_has_inspect_state_retry_default(self) -> None:
        self.client.account_request(
            "issue_reset",
            {"user_id": "user-1", "expected_auth_revision": 4},
        )
        call = self.client.calls[0]
        self.assertEqual(
            "/api/v1/admin/users/user-1/password-reset",
            call["path"],
        )
        self.assertEqual("POST", call["method"])
        self.assertEqual({"expected_auth_revision": 4}, call["body"])
        self.assertIsNone(call.get("idempotency_key"))
        self.assertEqual(16 * 1024, call["maximum_body_bytes"])

    def test_invalid_arguments_fail_before_transport(self) -> None:
        invalid_requests = (
            ("unknown", {}),
            ("list", {"query": "reader"}),
            (
                "create",
                {
                    "login_name": "reader",
                    "display_name": "R",
                    "password": "x",
                    "idempotency_key": "account-key-1",
                },
            ),
            ("create", {"login_name": "reader", "display_name": "R"}),
            ("create", {"login_name": "reader", "display_name": "R", "idempotency_key": "short"}),
            (
                "update",
                {
                    "user_id": " ",
                    "expected_auth_revision": 1,
                    "changes": {"role": "user"},
                    "idempotency_key": "account-key-1",
                },
            ),
            (
                "update",
                {
                    "user_id": "u" * 65,
                    "expected_auth_revision": 1,
                    "changes": {"role": "user"},
                    "idempotency_key": "account-key-1",
                },
            ),
            (
                "update",
                {
                    "user_id": "u",
                    "expected_auth_revision": True,
                    "changes": {"role": "user"},
                    "idempotency_key": "account-key-1",
                },
            ),
            (
                "update",
                {
                    "user_id": "u",
                    "expected_auth_revision": 1,
                    "changes": {},
                    "idempotency_key": "account-key-1",
                },
            ),
            (
                "update",
                {
                    "user_id": "u",
                    "expected_auth_revision": 1,
                    "changes": {"password": "x"},
                    "idempotency_key": "account-key-1",
                },
            ),
            (
                "update",
                {
                    "user_id": "u",
                    "expected_auth_revision": 1,
                    "changes": {"is_active": 1},
                    "idempotency_key": "account-key-1",
                },
            ),
            (
                "issue_reset",
                {"user_id": "u", "expected_auth_revision": 1, "idempotency_key": "account-key-1"},
            ),
        )
        for action, arguments in invalid_requests:
            with self.subTest(action=action, keys=tuple(arguments)):
                with self.assertRaises(ValueError):
                    self.client.account_request(action, arguments)
        self.assertEqual([], self.client.calls)

    def test_oversized_utf8_body_fails_before_transport(self) -> None:
        with self.assertRaisesRegex(ValueError, "16 KiB"):
            self.client.account_request(
                "create",
                {
                    "login_name": "🧪" * 4096,
                    "display_name": "Reader",
                    "idempotency_key": "account-create-1",
                },
            )
        self.assertEqual([], self.client.calls)


class AccountInputAndToolTests(unittest.TestCase):
    def test_public_schemas_are_strict_and_registered(self) -> None:
        self.assertEqual({}, ListUsersInput.model_validate({}).model_dump())
        self.assertEqual(
            "user",
            CreateUserInput(
                login_name="reader",
                display_name="Reader",
                idempotency_key="account-create-1",
            ).role,
        )
        self.assertTrue(
            set(
                (ListUsersInput, CreateUserInput, UpdateUserInput, IssueUserPasswordResetInput)
            ).issubset(TOOL_INPUT_MODELS)
        )
        with self.assertRaises(ValidationError):
            ListUsersInput.model_validate({"password": "x"})
        with self.assertRaises(ValidationError):
            CreateUserInput.model_validate(
                {
                    "login_name": "reader",
                    "display_name": "Reader",
                    "idempotency_key": "account-create-1",
                    "role": "owner",
                }
            )
        with self.assertRaises(ValidationError):
            UpdateUserInput.model_validate(
                {
                    "user_id": "user-1",
                    "expected_auth_revision": True,
                    "changes": {"role": "user"},
                    "idempotency_key": "account-update-1",
                }
            )
        with self.assertRaises(ValidationError):
            UpdateUserInput.model_validate(
                {
                    "user_id": "user-1",
                    "expected_auth_revision": 3,
                    "changes": {"is_active": 1},
                    "idempotency_key": "account-update-1",
                }
            )
        with self.assertRaises(ValidationError):
            IssueUserPasswordResetInput.model_validate(
                {
                    "user_id": "user-1",
                    "expected_auth_revision": 3,
                    "idempotency_key": "account-reset-1",
                }
            )

    def test_list_users_rejects_non_boolean_mode_and_invalid_account_shapes(self) -> None:
        invalid_modes: tuple[tuple[str, Any], ...] = (
            ("integer zero", 0),
            ("integer one", 1),
            ("null", None),
            ("string false", "false"),
            ("empty list", []),
            ("boolean list", [True]),
        )
        for label, mode in invalid_modes:
            with self.subTest(mode=label):
                application = _FakeAccountApplication()
                application.list_response["named_user_access"] = mode
                with self.assertRaises(PandratorMcpError) as caught:
                    list_users(_runtime(application), ListUsersInput())
                self.assertEqual("downstream_unavailable", caught.exception.code)

        application = _FakeAccountApplication()
        del application.list_response["named_user_access"]
        with self.assertRaises(PandratorMcpError) as caught:
            list_users(_runtime(application), ListUsersInput())
        self.assertEqual("downstream_unavailable", caught.exception.code)

        malformed_accounts = (
            {key: value for key, value in _ACCOUNT.items() if key != "login_name"},
            {**_ACCOUNT, "is_active": 1},
        )
        for account in malformed_accounts:
            with self.subTest(account=account):
                application = _FakeAccountApplication()
                application.list_response["items"] = [account]
                with self.assertRaises(PandratorMcpError) as caught:
                    list_users(_runtime(application), ListUsersInput())
                self.assertEqual("downstream_unavailable", caught.exception.code)

    def test_safe_error_codes_are_passthrough_and_typed(self) -> None:
        self.assertTrue(set(_ACCOUNT_CODES).issubset(_PASSTHROUGH_ERROR_CODES))
        self.assertTrue(set(_ACCOUNT_CODES).issubset(set(get_args(FailureCode))))
        self.assertTrue(issubclass(ApplicationClient, ApplicationAccountMethods))

    def test_direct_handlers_whitelist_responses_and_validate_reset_url(self) -> None:
        application = _FakeAccountApplication()
        runtime = _runtime(application)
        listed = list_users(runtime, ListUsersInput()).result
        self.assertEqual(
            {
                "items": [{key: _ACCOUNT[key] for key in _PUBLIC_ACCOUNT_FIELDS}],
                "initial_user_id": "initial-admin",
                "named_user_access": False,
            },
            listed,
        )
        created = create_user(
            runtime,
            CreateUserInput(
                login_name="reader",
                display_name="Reader",
                idempotency_key="account-create-1",
            ),
        ).result
        self.assertEqual(_ACCOUNT, created)
        updated = update_user(
            runtime,
            UpdateUserInput(
                user_id="user-1",
                expected_auth_revision=3,
                changes=UserAccountChanges(is_active=False),
                idempotency_key="account-update-1",
            ),
        ).result
        self.assertEqual(_ACCOUNT, updated)
        self.assertEqual(False, application.calls[-1][1]["changes"]["is_active"])
        reset = issue_user_password_reset(
            runtime,
            IssueUserPasswordResetInput(user_id="user-1", expected_auth_revision=3),
        ).result
        self.assertEqual(
            {
                "grant_id": "grant-1",
                "user_id": "user-1",
                "expires_at": "2026-10-23T12:30:00Z",
                "reset_path": "/password-reset#token=" + "A" * 43,
                "reset_url": "https://app.example/password-reset#token=" + "A" * 43,
            },
            reset,
        )
        self.assertNotIn("must-not-escape", json.dumps([listed, created, updated]))
        self.assertNotIn("must-not-escape", json.dumps(reset))
        self.assertEqual(
            ["list", "create", "update", "issue_reset"], [item[0] for item in application.calls]
        )
        self.assertNotIn("idempotency_key", application.calls[-1][1])

        unsafe_paths = (
            "https://evil.example/password-reset#token=" + "A" * 43,
            "//evil.example/password-reset#token=" + "A" * 43,
            "/other#token=" + "A" * 43,
            "/password-reset?token=" + "A" * 43,
            "/password-reset#token=" + "A" * 42,
            "/password-reset#token=" + "A" * 43 + "&next=/admin",
        )

        class UnsafeResetApplication(_FakeAccountApplication):
            def __init__(self, reset_path: str) -> None:
                super().__init__()
                self.reset_path = reset_path

            def account_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
                payload = super().account_request(action, arguments)
                if action == "issue_reset":
                    payload["reset_path"] = self.reset_path
                return payload

        for reset_path in unsafe_paths:
            with (
                self.subTest(reset_path=reset_path),
                self.assertRaises(PandratorMcpError) as caught,
            ):
                issue_user_password_reset(
                    _runtime(UnsafeResetApplication(reset_path)),
                    IssueUserPasswordResetInput(user_id="user-1", expected_auth_revision=3),
                )
            self.assertEqual("downstream_unavailable", caught.exception.code)


@unittest.skipIf(Client is None, "The standalone MCP SDK dependency is not installed.")
class AccountMcpRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_list_tool_preserves_enabled_and_solo_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="missing",
                    configuration_path=Path(directory) / "missing.json",
                )
            )
            application = _FakeAccountApplication()
            runtime.application = cast(Any, application)
            runtime.startup_error = None
            server = build_server(runtime)
            assert Client is not None

            async with Client(server, mode="auto", raise_exceptions=False) as client:
                for mode in (True, False):
                    with self.subTest(named_user_access=mode):
                        application.list_response["named_user_access"] = mode
                        result = await client.call_tool("pandrator_list_users", {})
                        self.assertFalse(result.is_error)
                        listed = result.structured_content["result"]
                        self.assertIs(mode, listed["named_user_access"])
                        self.assertEqual("initial-admin", listed["initial_user_id"])
                        self.assertEqual(
                            [{key: _ACCOUNT[key] for key in _PUBLIC_ACCOUNT_FIELDS}],
                            listed["items"],
                        )
                        self.assertNotIn("must-not-escape", json.dumps(result.structured_content))

    async def test_catalog_schemas_annotations_and_response_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_runtime(
                McpSettings(
                    target_name="missing",
                    configuration_path=Path(directory) / "missing.json",
                )
            )
            application = _FakeAccountApplication()
            runtime.application = cast(Any, application)
            runtime.startup_error = None
            server = build_server(runtime)
            assert Client is not None

            expected = {
                "pandrator_list_users": (
                    RiskClass.READ,
                    "ListUsersInput",
                    "app.admin",
                    None,
                    False,
                    "listUsers",
                    "GET",
                    "/api/v1/admin/users",
                    "List users",
                ),
                "pandrator_create_user": (
                    RiskClass.WRITE,
                    "CreateUserInput",
                    "app.admin",
                    False,
                    True,
                    "createUser",
                    "POST",
                    "/api/v1/admin/users",
                    "Create user",
                ),
                "pandrator_update_user": (
                    RiskClass.WRITE,
                    "UpdateUserInput",
                    "app.admin",
                    True,
                    True,
                    "updateUser",
                    "PATCH",
                    "/api/v1/admin/users/{userId}",
                    "Update user",
                ),
                "pandrator_issue_user_password_reset": (
                    RiskClass.WRITE,
                    "IssueUserPasswordResetInput",
                    "app.admin",
                    True,
                    False,
                    "issueUserPasswordReset",
                    "POST",
                    "/api/v1/admin/users/{userId}/password-reset",
                    "Create password setup link",
                ),
            }
            tool_descriptions = {
                "pandrator_list_users": "List accounts, Administrator/User roles and password setup status. Requires an Administrator application credential. Project access is separate.",
                "pandrator_create_user": "Create an account with a private personal workspace. User is the default role; Administrator must be chosen explicitly. Issue a password setup link separately. Requires an Administrator credential.",
                "pandrator_update_user": "Change an account display name, role or enabled state at its expected authentication revision. Role/state changes sign out affected credentials. Requires an Administrator credential; initial Administrator retirement remains protected.",
                "pandrator_issue_user_password_reset": "Create a secret, one-use password setup link valid for 30 minutes. Reissuing replaces earlier links. Return it to the operator for private manual delivery; do not send it automatically. Requires an Administrator credential. The recipient chooses their password in the browser.",
            }
            async with Client(server, mode="auto", raise_exceptions=True) as client:
                listed_tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                for name, (
                    risk,
                    input_model,
                    scope,
                    destructive,
                    idempotency,
                    operation,
                    method,
                    path,
                    title,
                ) in expected.items():
                    action = ACTION_CATALOG.get(name)
                    self.assertEqual(risk, action.risk)
                    self.assertEqual(input_model, action.input_model)
                    self.assertEqual(scope, action.required_scope)
                    self.assertEqual(idempotency, action.requires_idempotency)
                    self.assertEqual(operation, action.downstream_operation_id)
                    self.assertEqual(method, action.method)
                    self.assertEqual(path, action.path)
                    self.assertFalse(action.requires_confirmation)
                    tool = listed_tools[name]
                    self.assertEqual(title, tool.title)
                    self.assertEqual(tool_descriptions[name], tool.description)
                    registered = server._tool_manager.get_tool(name)
                    assert registered is not None
                    assert registered.annotations is not None
                    self.assertEqual(destructive, registered.annotations.destructive_hint)
                    expected_idempotent_hint = (
                        None if name == "pandrator_list_users" else idempotency
                    )
                    self.assertEqual(
                        expected_idempotent_hint, registered.annotations.idempotent_hint
                    )
                    self.assertEqual(
                        not destructive and name == "pandrator_list_users",
                        registered.annotations.read_only_hint,
                    )
                    schema = tool.input_schema
                    self.assertEqual("object", schema["type"])
                    self.assertNotIn("password", schema["properties"])
                    self.assertNotIn("new_password", schema["properties"])
                    self.assertNotIn("token", schema["properties"])

                list_standard = await client.call_tool("pandrator_list_users", {})
                list_structured = await client.call_tool(
                    "pandrator_list_users", {"response_mode": "structured"}
                )
                self.assertEqual(
                    list_standard.structured_content["result"],
                    list_structured.structured_content["result"],
                )
                marker = json.loads(cast(Any, list_structured.content[0]).text)
                self.assertEqual("structuredContent", marker["data"])

                created = await client.call_tool(
                    "pandrator_create_user",
                    {
                        "login_name": "reader",
                        "display_name": "Reader",
                        "idempotency_key": "account-create-1",
                    },
                )
                self.assertFalse(created.is_error)
                self.assertEqual("user", application.calls[-1][1]["role"])
                updated = await client.call_tool(
                    "pandrator_update_user",
                    {
                        "user_id": "user-1",
                        "expected_auth_revision": 3,
                        "changes": {"is_active": False},
                        "idempotency_key": "account-update-1",
                    },
                )
                self.assertFalse(updated.is_error)
                self.assertIs(application.calls[-1][1]["changes"]["is_active"], False)
                reset = await client.call_tool(
                    "pandrator_issue_user_password_reset",
                    {"user_id": "user-1", "expected_auth_revision": 3},
                )
                self.assertFalse(reset.is_error)
                self.assertNotIn("idempotency_key", application.calls[-1][1])
                self.assertIn(
                    "https://app.example/password-reset#token=",
                    json.dumps(reset.structured_content),
                )
                self.assertNotIn("must-not-escape", json.dumps(reset.structured_content))
                reset_structured = await client.call_tool(
                    "pandrator_issue_user_password_reset",
                    {
                        "user_id": "user-1",
                        "expected_auth_revision": 3,
                        "response_mode": "structured",
                    },
                )
                self.assertFalse(reset_structured.is_error)
                self.assertEqual(
                    reset.structured_content["result"]["reset_url"],
                    reset_structured.structured_content["result"]["reset_url"],
                )
                self.assertNotIn(
                    "must-not-escape",
                    json.dumps(reset_structured.structured_content),
                )

                invalid = await client.call_tool(
                    "pandrator_update_user",
                    {
                        "user_id": "user-1",
                        "expected_auth_revision": 3,
                        "changes": {"password": "not-an-input"},
                        "idempotency_key": "account-update-2",
                    },
                )
                self.assertTrue(invalid.is_error)
                self.assertEqual(6, len(application.calls))
