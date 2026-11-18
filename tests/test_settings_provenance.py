"""Effective setting values and the layer that supplied each one."""

from pandrator.web.settings_policy import BUILTIN_DEFAULTS
from pandrator.web.workspace_settings import settings_provenance


def _snapshot(*, builtin=None, global_value=None, context=None, override=None, effective=None):
    return {
        "builtin": dict(builtin or {}),
        "global": dict(global_value or {}),
        "session_context": dict(context or {}),
        "override": dict(override or {}),
        "effective": dict(effective or {}),
    }


def test_builtin_is_the_base_layer():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed"},
        effective={"audio_mode": "mixed"},
    )

    assert settings_provenance(snapshot, ["audio_mode"]) == {
        "audio_mode": {"effective": "mixed", "origin": "builtin"}
    }


def test_each_layer_supplying_a_key_wins_in_order():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed"},
        global_value={"audio_mode": "preserve"},
        context={"audio_mode": "dubbing_only"},
        override={"audio_mode": "mixed"},
        effective={"audio_mode": "mixed"},
    )

    assert settings_provenance(snapshot, ["audio_mode"])["audio_mode"]["origin"] == (
        "session_override"
    )
    without_override = _snapshot(
        builtin={"audio_mode": "mixed"},
        global_value={"audio_mode": "preserve"},
        context={"audio_mode": "dubbing_only"},
        effective={"audio_mode": "dubbing_only"},
    )
    assert settings_provenance(without_override, ["audio_mode"])["audio_mode"] == {
        "effective": "dubbing_only",
        "origin": "session_context",
    }
    global_only = _snapshot(
        builtin={"audio_mode": "mixed"},
        global_value={"audio_mode": "preserve"},
        effective={"audio_mode": "preserve"},
    )
    assert settings_provenance(global_only, ["audio_mode"])["audio_mode"] == {
        "effective": "preserve",
        "origin": "global",
    }


def test_run_override_wins_and_reaches_the_effective_value():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed"},
        context={"audio_mode": "mixed"},
        effective={"audio_mode": "mixed"},
    )

    fields = settings_provenance(
        snapshot, ["audio_mode"], {"audio_mode": "dubbing_only"}
    )

    assert fields["audio_mode"] == {
        "effective": "dubbing_only",
        "origin": "run_override",
    }
    # The caller's snapshot is not mutated by the run override.
    assert snapshot["effective"] == {"audio_mode": "mixed"}


def test_a_value_the_resolver_rewrote_reports_clamped():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed"},
        context={"audio_mode": "mixed"},
        override={"audio_mode": "mixed"},
        # A voiceover source without audio is clamped back to dubbing only.
        effective={"audio_mode": "dubbing_only"},
    )

    assert settings_provenance(snapshot, ["audio_mode"])["audio_mode"] == {
        "effective": "dubbing_only",
        "origin": "clamped",
    }


def test_a_resolved_section_is_authoritative_over_the_run_override_overlay():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed"},
        context={"audio_mode": "mixed"},
        effective={"audio_mode": "mixed"},
    )

    fields = settings_provenance(
        snapshot,
        ["audio_mode"],
        {"audio_mode": "mixed"},
        {"audio_mode": "dubbing_only"},
    )

    assert fields["audio_mode"] == {
        "effective": "dubbing_only",
        "origin": "clamped",
    }


def test_a_resolved_section_still_names_the_supplying_layer():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed"},
        context={"audio_mode": "mixed"},
        effective={"audio_mode": "mixed"},
    )

    fields = settings_provenance(
        snapshot,
        ["audio_mode"],
        {"audio_mode": "dubbing_only"},
        {"audio_mode": "dubbing_only"},
    )

    assert fields["audio_mode"] == {
        "effective": "dubbing_only",
        "origin": "run_override",
    }


def test_reports_only_the_requested_keys():
    snapshot = _snapshot(
        builtin={"audio_mode": "mixed", "export_mode": "media"},
        effective={"audio_mode": "mixed", "export_mode": "media"},
    )

    assert list(settings_provenance(snapshot, ["export_mode"])) == ["export_mode"]


def test_an_unsupplied_key_falls_back_to_the_effective_value():
    snapshot = _snapshot(effective={})

    assert settings_provenance(snapshot, ["subtitle_selection"]) == {
        "subtitle_selection": {"effective": None, "origin": "builtin"}
    }


def test_service_and_provider_layers_sit_between_global_and_the_session():
    snapshot = _snapshot(
        builtin={"service": "audio_cpp"},
        global_value={"service": "openai"},
        context={"service": "context"},
        override={"service": "override"},
        effective={"service": "override"},
    )
    snapshot["service"] = {"service": "vertex_ai"}
    snapshot["provider"] = {"voice": "Algenib"}

    assert settings_provenance(snapshot, ["service"])["service"]["origin"] == (
        "session_override"
    )
    without_override = {
        **snapshot,
        "override": {},
        "session_context": {},
        "effective": {"service": "vertex_ai"},
    }
    assert settings_provenance(without_override, ["service"])["service"] == {
        "effective": "vertex_ai",
        "origin": "service",
    }


def test_a_provider_default_wins_over_the_global_layer():
    snapshot = _snapshot(
        builtin={"voice": "alloy"},
        global_value={"voice": "alloy"},
        effective={"voice": "Algenib"},
    )
    snapshot["service"] = {}
    snapshot["provider"] = {"voice": "Algenib"}

    assert settings_provenance(snapshot, ["voice"])["voice"] == {
        "effective": "Algenib",
        "origin": "provider",
    }


def test_a_key_the_resolver_added_reports_derived():
    snapshot = _snapshot(
        builtin={"max_chars_per_line": 42},
        effective={"max_chars_per_line": 42, "language_defaults": False},
    )

    assert settings_provenance(snapshot, ["language_defaults"]) == {
        "language_defaults": {"effective": False, "origin": "derived"}
    }
    # A key absent everywhere, including the effective section, stays builtin.
    assert settings_provenance(snapshot, ["max_cps"]) == {
        "max_cps": {"effective": None, "origin": "builtin"}
    }


def test_builtin_synchronization_speed_is_1_15():
    assert BUILTIN_DEFAULTS["audio"]["synchronization_speed"] == 1.15
