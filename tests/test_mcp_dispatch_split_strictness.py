"""Native split-boundary cue IDs preserve the domain's strict integer contract."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID
from pandrator_mcp.schemas import InspectDispatchSplitBoundariesInput
from pandrator_mcp.server import build_server
from tests.test_mcp_media_edit_registration import fixture_runtime

ARGUMENTS = {"batch_id": "batch-1", "lease_token": "fixture-lease", "cue_id": 1}
INVALID_IDS = [True, "1", 1.0]


@pytest.mark.parametrize("cue_id", INVALID_IDS, ids=["boolean", "numeric-string", "float"])
def test_domain_split_cue_ids_reject_coercion(cue_id: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        InspectDispatchSplitBoundariesInput.model_validate({**ARGUMENTS, "cue_id": cue_id})
    assert caught.value.errors()[0]["loc"] == ("cue_id",)


def test_domain_split_accepts_integer_cue_id() -> None:
    assert InspectDispatchSplitBoundariesInput.model_validate(ARGUMENTS).cue_id == 1


async def invoke(root: Path, cue_id: Any) -> tuple[Any, Any]:
    runtime, _, application = fixture_runtime(root)
    application.inspect_dispatch_split_boundaries.return_value = {
        "batch_id": "batch-1",
        "cue_id": 1,
        "boundaries": [],
    }
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    async with Client(build_server(runtime), raise_exceptions=False) as client:
        result = await client.call_tool(
            "pandrator_inspect_dispatch_split_boundaries", {**ARGUMENTS, "cue_id": cue_id}
        )
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    return result, application


@pytest.mark.parametrize("cue_id", INVALID_IDS, ids=["boolean", "numeric-string", "float"])
def test_native_split_cue_ids_reject_coercion(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], cue_id: Any
) -> None:
    result, application = asyncio.run(invoke(tmp_path, cue_id))
    assert application.mock_calls == []
    assert result.is_error
    assert capsys.readouterr().out == ""


def test_native_split_accepts_integer_cue_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result, application = asyncio.run(invoke(tmp_path, 1))
    assert not result.is_error
    application.inspect_dispatch_split_boundaries.assert_called_once_with(
        "batch-1", lease_token="fixture-lease", cue_id=1, offset=0, limit=30
    )
    assert len(application.mock_calls) == 1
    assert capsys.readouterr().out == ""
