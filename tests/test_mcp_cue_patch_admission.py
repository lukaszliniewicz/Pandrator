"""Cue patch ordinals obey their public DTO admission without truncation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pydantic import ValidationError

import pandrator_mcp.server as adapter
from pandrator_mcp.schemas import CuePatchInput
from tests.test_mcp_generic_dispatch_validation import failure_value
from tests.test_mcp_media_edit_registration import fixture_runtime

OMITTED = object()
MARKER = "private-ordinal-fixture-value"
BASE = {
    "session_id": "session-1",
    "stage": "transcribe",
    "expected_revision": 1,
    "idempotency_key": "cue:admission:1",
}


async def invoke(runtime: Any, ordinal: Any, protocol: str) -> Any:
    cue = {"text": "Fixture", "preview_metadata": "ignored"}
    if ordinal is not OMITTED:
        cue["ordinal"] = ordinal
    async with Client(
        adapter.build_server(runtime), mode=protocol, raise_exceptions=False
    ) as client:
        return await client.call_tool("pandrator_patch_subtitle_cues", {**BASE, "cues": [cue]})


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("ordinal", [1.5, "1.5", MARKER, {}, [], None, OMITTED, 0, -1])
def test_native_cue_ordinal_rejects_invalid_values_without_private_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, ordinal: Any
) -> None:
    with pytest.raises(ValidationError):
        CuePatchInput.model_validate({"ordinal": ordinal if ordinal is not OMITTED else None})
    runtime, calls, application = fixture_runtime(tmp_path)
    reached: list[Any] = []

    def handler(current: Any, arguments: Any) -> dict[str, Any]:
        reached.append(arguments)
        return {"fixture": "ok"}

    monkeypatch.setattr(adapter, "patch_subtitle_cues", handler)
    result = asyncio.run(invoke(runtime, ordinal, protocol))
    assert result.is_error and reached == calls == application.mock_calls == []
    failure = failure_value(result, "pandrator_patch_subtitle_cues", "ordinal")
    assert MARKER not in json.dumps(failure)


@pytest.mark.parametrize("protocol", ["2026-07-28", "legacy"], ids=["modern", "legacy"])
@pytest.mark.parametrize("ordinal", [1, 2, "1", 1.0, True])
def test_native_cue_ordinal_preserves_existing_dto_coercions_and_patch_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, ordinal: Any
) -> None:
    expected = CuePatchInput(
        ordinal=ordinal, text="Fixture", speaker=None, start_ms=None, end_ms=None
    )
    runtime, calls, application = fixture_runtime(tmp_path)
    reached: list[Any] = []

    def handler(current: Any, arguments: Any) -> dict[str, Any]:
        assert current is runtime
        reached.append(arguments)
        return {"fixture": "ok"}

    monkeypatch.setattr(adapter, "patch_subtitle_cues", handler)
    result = asyncio.run(invoke(runtime, ordinal, protocol))
    assert not result.is_error and len(reached) == 1
    cue = reached[0].cues[0]
    assert cue.model_dump() == expected.model_dump()
    assert cue.model_fields_set == expected.model_fields_set
    assert calls == application.mock_calls == []
