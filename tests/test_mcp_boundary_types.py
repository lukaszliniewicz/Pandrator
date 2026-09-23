"""Input boundary checks for MCP target profiles."""

import pytest
from pydantic import ValidationError

from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.targets import TargetProfile


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("requested_application_scopes", "Application scopes must be iterable"),
        ("manager_requested_scopes", "Manager recovery scopes must be iterable"),
        ("allowed_private_cidrs", "Allowed private CIDRs must be iterable"),
    ],
)
def test_truthy_noniterable_profile_values_are_validation_errors(field, message):
    with pytest.raises(ValidationError, match=message):
        TargetProfile.model_validate(
            {
                "name": "remote",
                "mode": TargetMode.PRIVATE_NETWORK,
                "application_origin": "https://pandrator.example",
                "allowed_private_cidrs": ["10.0.0.0/8"],
                field: 1,
            }
        )


@pytest.mark.parametrize("container", [list, tuple])
def test_valid_scope_and_cidr_containers_keep_normalization(container):
    profile = TargetProfile.model_validate(
        {
            "name": "remote",
            "mode": TargetMode.PRIVATE_NETWORK,
            "application_origin": "https://pandrator.example",
            "requested_application_scopes": container(["app.read", "app.read", "app.run"]),
            "manager_requested_scopes": container(["manager.read", "manager.runtime"]),
            "allowed_private_cidrs": container(["10.0.0.1/8", "10.0.0.0/8"]),
        }
    )
    assert profile.requested_application_scopes == ("app.read", "app.run")
    assert profile.manager_requested_scopes == ("manager.read", "manager.runtime")
    assert profile.allowed_private_cidrs == ("10.0.0.0/8",)
