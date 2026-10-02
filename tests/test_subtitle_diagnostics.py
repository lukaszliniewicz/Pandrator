"""Focused checks for compact finalization diagnostics and setting origins."""

from types import SimpleNamespace

from pandrator.logic.dubbing.subtitle_finalization import (
    diagnose_srt_content,
    finalize_srt_content,
)
from pandrator.web.export_subtitles import subtitle_profile
from pandrator.web.settings_policy import BUILTIN_DEFAULTS, adapt_runtime_settings
from pandrator.web.workspace_settings import subtitle_settings_provenance
from pandrator_mcp.schemas import GetSessionSettingsInput, ListArtifactsInput
from pandrator_mcp.tools.inventory import list_artifacts
from pandrator_mcp.tools.sessions import get_session_settings


def _snapshot(global_values=None, session_values=None):
    return {
        "builtin": BUILTIN_DEFAULTS["subtitles"],
        "global": global_values or {},
        "session_context": {},
        "override": session_values or {},
    }


def test_cjk_profiles_and_inherited_custom_limit_origins():
    defaults = subtitle_settings_provenance(_snapshot())
    settings = adapt_runtime_settings("subtitles", BUILTIN_DEFAULTS["subtitles"])
    assert [subtitle_profile(settings, defaults, language=code)["limits"]["max_chars_per_second"]["effective"] for code in ("ja", "zh", "ko")] == [7.0, 9.0, 12.0]
    assert subtitle_profile(settings, defaults, language="ja")["limits"]["max_chars_per_line"]["effective"] == 16

    snapshot = _snapshot({"max_chars_per_line": 42}, {"max_cps": 15})
    origins = subtitle_settings_provenance(snapshot)
    flag = origins["fields"]["language_defaults"]
    assert flag["origin"] == "derived_from_custom_limits"
    assert flag["causes"] == [
        {"field": "max_chars_per_line", "origin": "global"},
        {"field": "max_cps", "origin": "session_override"},
    ]
    disabled = {**settings, "subtitle_language_defaults": False, "subtitle_max_chars_per_line": 42, "subtitle_max_cps": 15}
    profile = subtitle_profile(disabled, origins, language="ja")
    assert profile["warning"]["code"] == "cjk_language_defaults_disabled"
    assert profile["limits"]["max_chars_per_line"]["effective"] == 42
    assert profile["limits"]["max_chars_per_second"]["effective"] == 15

    explicit = subtitle_settings_provenance(_snapshot({"max_chars_per_line": 42, "language_defaults": True}))
    assert explicit["fields"]["language_defaults"]["origin"] == "global"
    assert subtitle_profile({**disabled, "subtitle_language_defaults": True}, explicit, language="ja")["warning"] is None


def test_structured_and_flat_run_origins_and_clamping():
    origins = subtitle_settings_provenance(
        _snapshot(), structured={"max_chars_per_line": 200},
        flat={"subtitle_max_cps": 50},
    )
    fields = origins["fields"]
    assert fields["max_chars_per_line"]["origin"] == "structured_run_override"
    assert fields["max_cps"]["origin"] == "flat_run_override"
    assert fields["language_defaults"]["origin"] == "derived_from_custom_limits"
    assert fields["language_defaults"]["causes"] == [{"field": "max_cps", "origin": "flat_run_override"}]
    profile = subtitle_profile(
        {"subtitle_language_defaults": False, "subtitle_max_chars_per_line": 200, "subtitle_max_cps": 50},
        origins, language="ja",
    )
    assert profile["limits"]["max_chars_per_line"]["supplied"] == 200
    assert profile["limits"]["max_chars_per_line"]["effective"] == 100
    assert profile["limits"]["max_chars_per_second"]["effective"] == 40


def test_counts_invalid_duration_and_residual_speed_violation_without_changing_output():
    content = "1\n00:00:00,000 --> 00:00:00,000\nInvalid duration\n\n2\n00:00:01,000 --> 00:00:01,100\nSupercalifragilisticexpialidocious\n"
    settings = {"subtitle_max_chars_per_line": 8, "subtitle_max_cps": 1}
    before = finalize_srt_content(content, settings)
    input_counts = diagnose_srt_content(content, settings)
    final_counts = diagnose_srt_content(before, settings)
    assert input_counts["invalid_duration_cues"] == 1
    assert input_counts["reading_speed_violating_cues"] == 1
    assert input_counts["line_length_violating_cues"] == 2
    assert final_counts["reading_speed_violating_cues"] >= 1
    assert finalize_srt_content(content, settings) == before


def test_colliding_normalized_spans_keep_valid_cue_speed_in_both_orders():
    valid = "00:00:00,000 --> 00:00:00,100\nLong valid cue"
    invalid = "00:00:00,000 --> 00:00:00,000\nx"
    for first, second in ((valid, invalid), (invalid, valid)):
        content = f"1\n{first}\n\n2\n{second}\n"
        counts = diagnose_srt_content(content, {"subtitle_max_cps": 20})
        assert counts["cue_count"] == 2
        assert counts["invalid_duration_cues"] == 1
        assert counts["reading_speed_violating_cues"] == 1
        assert counts["observed_max_cps"] > 20


def test_mcp_projects_only_safe_subtitle_diagnostics_and_settings():
    provenance = subtitle_settings_provenance(_snapshot({"max_chars_per_line": 40}))
    profile = subtitle_profile(
        {"subtitle_language_defaults": False, "subtitle_max_chars_per_line": 40},
        provenance, language="ja",
    )
    diagnostic = {
        "profile": profile,
        "input": {"cue_count": 2, "reading_speed_violating_cues": 1},
        "final": {"cue_count": 3, "reading_speed_violating_cues": 0},
        "relative_path": "private/path.srt",
    }
    class Application:
        def get_session_settings(self, _session_id, _section):
            return {
                "section": "subtitles", "effective": {"max_cps": 20},
                "subtitle_settings_provenance": provenance,
                "subtitle_profiles": {"source": profile},
                "global": {"api_key": "secret"},
            }

        def list_artifacts(self, **_kwargs):
            return {"items": [
                {"id": "new", "kind": "srt", "metadata_json": {
                    "language": "ja", "subtitle_diagnostics": diagnostic,
                    "secret": "do not project",
                }},
                {"id": "old", "kind": "srt", "metadata_json": {}},
            ]}

    runtime = SimpleNamespace(require_application=lambda: Application())
    settings = get_session_settings(
        runtime, GetSessionSettingsInput(session_id="s", section="subtitles")
    )
    assert settings["subtitle_settings_provenance"] == provenance
    assert "global" not in settings
    artifacts = list_artifacts(runtime, ListArtifactsInput())
    new, old = artifacts["items"]
    assert new["subtitle_diagnostics"]["final"]["cue_count"] == 3
    assert "relative_path" not in str(new)
    assert "secret" not in str(new)
    assert old["subtitle_diagnostics"] == {"status": "unavailable"}
