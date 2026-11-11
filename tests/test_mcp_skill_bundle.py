"""Checks that the bundled Pandrator workflow skill matches its MCP contracts."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import get_args

import pytest

from pandrator_mcp.schemas.generation import GenerateSpeechPlanInput, RegenerateSegmentsInput

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "pandrator_mcp" / "skills" / "pandrator-workflows"


def test_skill_entrypoint_metadata_and_reference_links() -> None:
    body = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    assert body.startswith("---\nname: pandrator-workflows\n")
    assert "\ndescription: " in body.split("---", 2)[1]
    links = set(re.findall(r"\]\((references/[^)#]+)(?:#[^)]*)?\)", body))
    assert links
    for rel in links:
        assert (SKILL / rel).is_file(), rel


def test_skill_icons_and_wheel_package_data() -> None:
    metadata = (SKILL / "agents" / "openai.yaml").read_text(encoding="utf-8")
    for key in ("icon_small", "icon_large"):
        match = next(
            (
                line.split(":", 1)[1].strip().strip(chr(34))
                for line in metadata.splitlines()
                if line.strip().startswith(f"{key}:")
            ),
            None,
        )
        assert match, f"Missing {key} metadata"
        assert (SKILL / match).is_file(), key
    assert 'xmlns="http://www.w3.org/2000/svg"' in (SKILL / "assets" / "icon.svg").read_text()
    config = tomllib.loads((ROOT / "pandrator_mcp" / "pyproject.toml").read_text())
    assert "skills/**" in config["tool"]["setuptools"]["package-data"]["pandrator_mcp"]


@pytest.mark.parametrize("model", [RegenerateSegmentsInput, GenerateSpeechPlanInput])
def test_historical_generation_selector_matches_documentation(model: type) -> None:
    assert {"settings_source", "settings_source_run_id"} <= set(model.model_fields)
    choices = set(get_args(model.model_fields["settings_source"].annotation))
    assert choices == {"current", "run", "matching_run"}
    docs = (SKILL / "references" / "mcp-operations.md").read_text(encoding="utf-8")
    assert "`settings_source_run_id`" in docs
    assert '`settings_source="matching_run"`' in docs
    assert "settings_generation_run_id" not in docs
    assert 'settings_source="matching"' not in docs


def test_structured_payload_requires_structured_host() -> None:
    entrypoint = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    reference = (SKILL / "references" / "semantic-dispatch.md").read_text(encoding="utf-8")
    ops = (SKILL / "references" / "mcp-operations.md").read_text(encoding="utf-8")
    recovery = (SKILL / "references" / "recovery.md").read_text(encoding="utf-8")
    for doc in (entrypoint, reference, ops, recovery):
        assert "`structuredContent`" in doc
        assert "`standard`" in doc
        assert "host" in doc.lower()


def test_recent_release_semantics_are_documented_without_fake_mcp_tools() -> None:
    docs = {
        name: (SKILL / name).read_text(encoding="utf-8")
        for name in (
            "references/mcp-operations.md",
            "references/recovery.md",
            "references/quick-transcription.md",
            "references/semantic-dispatch.md",
        )
    }
    operations = docs["references/mcp-operations.md"]
    assert "early-finish" in operations
    assert "no_effect" in operations and "pass_completed" in operations
    assert "not** through a dedicated MCP" in operations
    assert '`subtitle_selection="dual"`' in operations
    assert '`subtitle_mode="soft"`' in operations
    assert '`subtitle_mode="burned"`' in operations
    assert "media_edit_word_timing_incomplete" in operations
    assert "current reviewed media-edit render" in operations
    assert "Local model files" in docs["references/quick-transcription.md"]
    assert "browser's subtitle review" in docs["references/semantic-dispatch.md"]


def test_named_mcp_tools_exist_in_actual_server_registry(tmp_path: Path) -> None:
    """Detect tool renames without requiring a running Pandrator application."""
    import asyncio

    from mcp import Client

    from pandrator_mcp.context import build_runtime
    from pandrator_mcp.server import build_server
    from pandrator_mcp.settings import McpSettings

    mentioned = set()
    for page in SKILL.rglob("*.md"):
        mentioned.update(re.findall(r"\bpandrator_[a-z0-9_]+\b", page.read_text()))

    runtime = build_runtime(
        McpSettings(
            target_name="unconfigured",
            configuration_path=tmp_path / "missing-target.json",
        )
    )

    async def registered() -> set[str]:
        async with Client(build_server(runtime), raise_exceptions=True) as client:
            return {item.name for item in (await client.list_tools()).tools}

    available = asyncio.run(registered())
    assert mentioned <= available, sorted(mentioned - available)
