"""Contract tests for the fixed MCP public-link request adapter."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pandrator_mcp.clients.application_public_links import (
    ApplicationPublicLinkMethods,
    _request_body,
)
from pandrator_mcp.schemas.public_links import (
    PUBLIC_LINK_ACTION_INPUT_MODELS,
    PUBLIC_LINK_INPUT_MODELS,
    CreateProjectLinkInput,
    ListLinkSubmissionsInput,
    PublicSharingSettingsInput,
    ReviewLinkSubmissionInput,
)

_REQUEST_UUID = "019a1000-0000-7000-8000-000000000001"
_IDEMPOTENCY_UUID = "019a1000-0000-7000-8000-000000000002"


class Recorder(ApplicationPublicLinkMethods):
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.response = response or {"ok": True}

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        call = {"path": path, **kwargs}
        if kwargs.get("idempotency_key") is not None:
            call["headers"] = {"Idempotency-Key": kwargs["idempotency_key"]}
        self.calls.append(call)
        return self.response

    def _request_binary_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Public-link commands use fixed JSON routes.")


def test_fixed_requests_use_only_owner_and_review_routes() -> None:
    client = Recorder()
    cases = (
        (
            "get_public_sharing_settings",
            {},
            "/api/v1/admin/public-sharing",
            "GET",
            None,
            None,
            None,
        ),
        (
            "configure_public_sharing",
            {"expected_revision": 0, "enabled": True, "public_origin": "https://public.invalid"},
            "/api/v1/admin/public-sharing",
            "PUT",
            None,
            {"expected_revision": 0, "enabled": True, "public_origin": "https://public.invalid"},
            None,
        ),
        (
            "list_project_links",
            {"project_id": "project/one?x"},
            "/api/v1/projects/project%2Fone%3Fx/links",
            "GET",
            None,
            None,
            None,
        ),
        (
            "create_project_link",
            {
                "project_id": "project/one",
                "label": "Review the draft",
                "mode": "review_translation",
                "expires_in_days": 12,
                "target_artifact_id": "artifact/one",
                "request_key": _REQUEST_UUID,
            },
            "/api/v1/projects/project%2Fone/links",
            "POST",
            None,
            {
                "label": "Review the draft",
                "mode": "review_translation",
                "expires_in_days": 12,
                "target_artifact_id": "artifact/one",
                "request_key": _REQUEST_UUID,
            },
            None,
        ),
        (
            "revoke_project_link",
            {"link_id": "link/one", "expected_revision": 4},
            "/api/v1/project-links/link%2Fone/revoke",
            "POST",
            None,
            {"expected_revision": 4},
            None,
        ),
        (
            "list_link_submissions",
            {"project_id": "project/one", "status": "accepted", "offset": 5, "limit": 10},
            "/api/v1/projects/project%2Fone/link-submissions",
            "GET",
            {"status": "accepted", "offset": 5, "limit": 10},
            None,
            None,
        ),
        (
            "get_link_submission",
            {"submission_id": "submission/one", "offset": 2, "limit": 75},
            "/api/v1/link-submissions/submission%2Fone",
            "GET",
            {"offset": 2, "limit": 75},
            None,
            None,
        ),
        (
            "review_link_submission",
            {
                "submission_id": "submission/one",
                "action": "accept",
                "expected_revision": 3,
                "expected_source_hash": "a" * 64,
                "expected_document_revision": 7,
                "expected_selected_artifact_id": "artifact/one",
                "idempotency_key": _IDEMPOTENCY_UUID,
            },
            "/api/v1/link-submissions/submission%2Fone/accept",
            "POST",
            None,
            {
                "expected_revision": 3,
                "expected_source_hash": "a" * 64,
                "expected_document_revision": 7,
                "expected_selected_artifact_id": "artifact/one",
            },
            {"Idempotency-Key": _IDEMPOTENCY_UUID},
        ),
    )

    for action, arguments, path, method, parameters, body, headers in cases:
        assert client.public_link_request(action, arguments) == {"ok": True}
        call = client.calls[-1]
        assert call["path"] == path
        assert call["method"] == method
        assert call["read_only_operation"] is (method == "GET")
        assert call["parameters"] == parameters
        assert call["body"] == body
        assert call["maximum_body_bytes"] == 8 * 1024
        assert call.get("headers") == headers
        if headers is None:
            assert call["idempotency_key"] is None
        else:
            assert call["idempotency_key"] == headers["Idempotency-Key"]


def test_defaults_and_reject_review_keep_requests_minimal() -> None:
    values = CreateProjectLinkInput.model_validate(
        {
            "project_id": "project-one",
            "label": "Manage",
            "mode": "manage_project",
            "request_key": _REQUEST_UUID,
        }
    )
    assert values.expires_in_days == 7
    assert values.target_artifact_id is None

    listing = ListLinkSubmissionsInput.model_validate({"project_id": "project-one"})
    assert (listing.status, listing.offset, listing.limit) == ("pending", 0, 25)
    assert PublicSharingSettingsInput(
        expected_revision=0,
        enabled=False,
        public_origin=None,
    ).public_origin is None

    client = Recorder()
    client.public_link_request(
        "review_link_submission",
        {
            "submission_id": "submission-one",
            "action": "reject",
            "expected_revision": 1,
            "idempotency_key": _IDEMPOTENCY_UUID,
        },
    )
    call = client.calls[-1]
    assert call["path"] == "/api/v1/link-submissions/submission-one/reject"
    assert call["body"] == {"expected_revision": 1}
    assert call["headers"] == {"Idempotency-Key": _IDEMPOTENCY_UUID}


def test_create_returns_the_server_url_once_without_rewriting_it() -> None:
    response = {"id": "link-one", "url": "https://example.invalid/one-time-link/REDACTED"}
    client = Recorder(response)
    result = client.public_link_request(
        "create_project_link",
        {
            "project_id": "project-one",
            "label": "Manage",
            "mode": "manage_project",
            "request_key": _REQUEST_UUID,
        },
    )
    assert result is response
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("model", "arguments"),
    [
        (PublicSharingSettingsInput, {"expected_revision": -1, "enabled": True, "public_origin": None}),
        (PublicSharingSettingsInput, {"expected_revision": True, "enabled": True, "public_origin": None}),
        (PublicSharingSettingsInput, {"expected_revision": 0, "enabled": 1, "public_origin": None}),
        (
            PublicSharingSettingsInput,
            {"expected_revision": 0, "enabled": True, "public_origin": "x" * 2049},
        ),
        (
            CreateProjectLinkInput,
            {
                "project_id": "project-one",
                "label": "Manage",
                "mode": "manage_project",
                "target_artifact_id": "artifact-one",
                "request_key": _REQUEST_UUID,
            },
        ),
        (
            CreateProjectLinkInput,
            {
                "project_id": "project-one",
                "label": "Review",
                "mode": "review_translation",
                "request_key": _REQUEST_UUID,
            },
        ),
        (
            CreateProjectLinkInput,
            {
                "project_id": "project-one",
                "label": "Review",
                "mode": "review_translation",
                "target_artifact_id": "artifact-one",
                "expires_in_days": 31,
                "request_key": _REQUEST_UUID,
            },
        ),
        (
            CreateProjectLinkInput,
            {
                "project_id": "project-one",
                "label": "x" * 81,
                "mode": "manage_project",
                "request_key": _REQUEST_UUID,
            },
        ),
        (
            CreateProjectLinkInput,
            {
                "project_id": "project-one",
                "label": "Manage",
                "mode": "manage_project",
                "request_key": "not-a-uuid",
            },
        ),
        (
            ReviewLinkSubmissionInput,
            {
                "submission_id": "submission-one",
                "action": "accept",
                "expected_revision": 1,
                "idempotency_key": _IDEMPOTENCY_UUID,
            },
        ),
        (
            ReviewLinkSubmissionInput,
            {
                "submission_id": "submission-one",
                "action": "accept",
                "expected_revision": 1,
                "expected_source_hash": "h" * 129,
                "expected_document_revision": 1,
                "expected_selected_artifact_id": "artifact-one",
                "idempotency_key": _IDEMPOTENCY_UUID,
            },
        ),
        (
            ReviewLinkSubmissionInput,
            {
                "submission_id": "submission-one",
                "action": "reject",
                "expected_revision": 1,
                "expected_source_hash": None,
                "idempotency_key": _IDEMPOTENCY_UUID,
            },
        ),
        (
            ReviewLinkSubmissionInput,
            {
                "submission_id": "submission-one",
                "action": "accept",
                "expected_revision": 1,
                "expected_source_hash": "hash",
                "expected_document_revision": True,
                "expected_selected_artifact_id": "artifact-one",
                "idempotency_key": _IDEMPOTENCY_UUID,
            },
        ),
    ],
)
def test_inputs_enforce_bounds_and_mode_specific_guards(model: Any, arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(arguments)


def test_list_and_detail_pagination_bounds_are_strict() -> None:
    with pytest.raises(ValidationError):
        ListLinkSubmissionsInput.model_validate({"project_id": "project-one", "limit": 51})
    with pytest.raises(ValidationError):
        ListLinkSubmissionsInput.model_validate({"project_id": "project-one", "offset": -1})
    with pytest.raises(ValidationError):
        ListLinkSubmissionsInput.model_validate({"project_id": "project-one", "status": "all"})
    with pytest.raises(ValidationError):
        ReviewLinkSubmissionInput.model_validate(
            {
                "submission_id": "submission-one",
                "action": "reject",
                "expected_revision": 1,
                "idempotency_key": _IDEMPOTENCY_UUID,
                "extra_destination": "https://example.invalid",
            }
        )


def test_request_body_serializer_enforces_eight_kibibytes() -> None:
    with pytest.raises(ValueError, match="8 KiB"):
        _request_body({"bounded": "x" * (8 * 1024)})


def test_invalid_actions_and_arguments_fail_before_transport() -> None:
    client = Recorder()
    with pytest.raises(ValueError, match="Unknown public-link action"):
        client.public_link_request("redeem_public_link", {})
    with pytest.raises(ValueError, match="Unknown public-link action"):
        client.public_link_request([], {})  # type: ignore[arg-type]
    for action, values in (
        ("get_public_sharing_settings", {"destination": "https://example.invalid"}),
        ("configure_public_sharing", {"expected_revision": 1, "enabled": True, "public_origin": None, "url": "https://example.invalid"}),
        ("list_project_links", {"project_id": "x" * 37}),
    ):
        with pytest.raises(ValidationError):
            client.public_link_request(action, values)
    assert client.calls == []


def test_input_mapping_covers_exact_action_set_with_extra_forbidden_models() -> None:
    assert set(PUBLIC_LINK_ACTION_INPUT_MODELS) == {
        "get_public_sharing_settings",
        "configure_public_sharing",
        "list_project_links",
        "create_project_link",
        "revoke_project_link",
        "list_link_submissions",
        "get_link_submission",
        "review_link_submission",
    }
    assert set(PUBLIC_LINK_ACTION_INPUT_MODELS.values()) == set(PUBLIC_LINK_INPUT_MODELS)
    assert all(model.model_json_schema()["additionalProperties"] is False for model in PUBLIC_LINK_INPUT_MODELS)
