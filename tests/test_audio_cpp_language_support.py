"""Language support stays package- and operation-scoped in the audio.cpp catalogue."""

from __future__ import annotations

import re

import pandrator.logic.audio_cpp_catalogue as catalogue
import pandrator.logic.language_capabilities as language_capabilities
from pandrator.logic.language_capabilities import registry_snapshot


def _support(model_id: str, operation: str = "tts") -> dict:
    metadata = catalogue.package_metadata(model_id)
    return metadata["language_support_by_operation"][operation]


def test_package_language_mapping_does_not_copy_the_complete_registry(monkeypatch):
    registry = language_capabilities._registry_data()
    original_deepcopy = language_capabilities.copy.deepcopy

    def guarded_deepcopy(value, memo=None):
        assert value is not registry, "Package mapping copied the full language registry"
        return original_deepcopy(value, memo)

    monkeypatch.setattr(language_capabilities.copy, "deepcopy", guarded_deepcopy)
    for model_id, count in (("fish_audio_s2_pro_q8_0", 83), ("omnivoice_q8_0", 646)):
        first = _support(model_id)
        assert len(first["languages"]) == count
        expected = list(first["languages"])
        first["languages"].clear()
        assert _support(model_id)["languages"] == expected


def test_language_mapping_preserves_aliases_sentinels_and_unregistered_tags():
    assert catalogue._mapped_languages(
        ["English", "pt_BR", "automatic", "und", "unknown", "detect", "qzz", "en-zz", "", None]
    ) == (["en", "pt-br"], [], ["und", "qzz", "en-zz"], True)
    assert catalogue._mapped_languages(None) == ([], [], [], False)
    assert catalogue._mapped_languages([]) == ([], [], [], False)


def test_pinned_source_records_bind_exact_packages_and_keep_source_revisions():
    expected = {
        "omnivoice_q8_0": ("omnivoice646", "exact", 646),
        "fish_audio_s2_pro_q8_0": ("fish_s2_pro83", "claim", 83),
        "higgs_audio_tts_4b_q8_0": ("higgs_v3_tts4b102", "exact", 102),
        "dots_tts_mf_q8_0": ("dots_tts24", "subset", 24),
    }
    packages = {row["id"]: row for row in catalogue.inventory()["packages"]}
    for model_id, (source_key, coverage, count) in expected.items():
        metadata = catalogue.package_metadata(model_id)
        record = metadata["language_support"]
        package = packages[model_id]
        manifest_revision = package.get("weight_manifest", {}).get("revision")

        assert record["source_key"] == source_key
        assert record["coverage"] == coverage
        assert len(record["languages"]) == count
        assert record["model_id"] == model_id
        assert record["model_revision"] == (manifest_revision or model_id)
        assert record["source_revision"] != record["model_revision"]
        assert record["runtime_requirement"] == catalogue.inventory()["runtime_version"]
        assert record["native_route"].startswith("audio_cpp:tts:")
        assert metadata["supported_languages"] == record["languages"]

    fish = _support("fish_audio_s2_pro_q8_0")
    assert fish["request_aliases"] == {"jv": "jw"}
    assert len(fish["languages"]) == len(fish["native_language_codes"]) == 83

    language_curation = catalogue.curation()["language_support"]
    expected_source_bound_models = {
        package["id"]
        for package in packages.values()
        if package["family"] in {"omnivoice", "fish_audio", "higgs_audio_tts"}
        or (
            package["family"] == "dots_tts"
            and package["id"].startswith(("dots_tts_mf_", "dots_tts_soar_"))
        )
    }
    actual_source_bound_models = {
        model_id
        for model_id, config in language_curation["models"].items()
        if config.get("source_key")
    }
    assert actual_source_bound_models == expected_source_bound_models
    assert not any(config.get("source_key") for config in language_curation["families"].values())


def test_dots_edit_is_unknown_audio_edit_and_does_not_inherit_tts_claim():
    normal = catalogue.package_metadata("dots_tts_mf_q8_0")
    editing = catalogue.package_metadata("dots_tts_edit_q8_0")

    assert normal["language_support"]["coverage"] == "subset"
    assert len(normal["supported_languages"]) == 24
    assert list(editing["language_support_by_operation"]) == ["audio_edit"]
    edit_record = editing["language_support"]
    assert edit_record["coverage"] == "unknown"
    assert edit_record["languages"] == []
    assert edit_record["operation"] == "audio_edit"
    assert edit_record["native_route"] == "audio_cpp:edit:dots_tts"


def test_voice_design_has_a_distinct_route_and_keeps_package_scoped_tts_evidence():
    model_id = "omnivoice_q8_0"
    metadata = catalogue.package_metadata(model_id)
    tts = metadata["language_support_by_operation"]["tts"]
    design = metadata["language_support_by_operation"]["voice_design"]
    package = next(row for row in catalogue.inventory()["packages"] if row["id"] == model_id)

    assert list(metadata["language_support_by_operation"]) == ["tts", "voice_design"]
    assert metadata["language_support"] == tts
    assert tts["operation"] == "tts"
    assert design["operation"] == "voice_design"
    assert tts["native_route"] == "audio_cpp:tts:omnivoice"
    assert design["native_route"] == "audio_cpp:design:omnivoice"
    assert design["model_id"] == tts["model_id"] == model_id
    expected_revision = package.get("weight_manifest", {}).get("revision") or model_id
    assert design["model_revision"] == tts["model_revision"] == expected_revision
    assert design["source_key"] == tts["source_key"] == "omnivoice646"
    assert design["source_revision"] == tts["source_revision"]
    assert design["coverage"] == tts["coverage"] == "exact"
    assert design["languages"] == tts["languages"]
    assert design["native_language_codes"] == tts["native_language_codes"]


def test_design_only_and_vdes_tasks_keep_tts_compatibility_without_asr_conflation():
    model_id = "moss_voicegen_bf16_codec_f16_decode"
    metadata = catalogue.package_metadata(model_id)
    tts = metadata["language_support_by_operation"]["tts"]
    design = metadata["language_support_by_operation"]["voice_design"]

    assert metadata["language_support"]["operation"] == "tts"
    assert tts["operation"] == "tts"
    assert design["operation"] == "voice_design"
    assert tts["native_route"] == "audio_cpp:design:moss_voicegen"
    assert design["native_route"] == "audio_cpp:design:moss_voicegen"
    assert tts["model_id"] == design["model_id"] == model_id
    assert tts["model_revision"] == design["model_revision"]

    package = {"id": "vdes-fixture", "family": "vdes_fixture"}
    family = {"id": "vdes_fixture", "tasks": ["vdes"], "languages": ["en"]}
    primary, records = catalogue._language_support(package, family, {"supported_languages": ["en"]})
    assert primary["operation"] == "tts"
    assert records["tts"]["native_route"] == "audio_cpp:vdes:vdes_fixture"
    assert records["voice_design"]["operation"] == "voice_design"
    assert records["voice_design"]["native_route"] == "audio_cpp:vdes:vdes_fixture"
    assert records["voice_design"]["model_id"] == records["tts"]["model_id"]

    asr = catalogue.package_metadata("qwen3_asr_0_6b_q8_0")
    assert "voice_design" not in asr["language_support_by_operation"]


def test_sanotts_and_index_tts_variants_keep_their_own_exact_language_lists():
    english_ids = (
        "sanotts_heart_orig",
        "sanotts_heart_nano_orig",
        "sanotts_amy_orig",
        "sanotts_hfc_orig",
        "sanotts_kristin_orig",
    )
    for model_id in english_ids:
        record = _support(model_id)
        assert record["coverage"] == "exact"
        assert record["languages"] == ["en"]

    assert _support("sanotts_cs_orig")["languages"] == ["cs"]
    assert catalogue.package_metadata("sanotts_cs_orig")["supported_languages"] == ["cs"]

    for model_id in (
        "index_tts2_f16",
        "index_tts2_orig",
        "index_tts2_q8_0",
        "index_tts2_safetensors",
    ):
        assert _support(model_id)["languages"] == ["en", "zh"]
    for model_id in ("index_tts2_5_f16", "index_tts2_5_orig", "index_tts2_5_q8_0"):
        assert _support(model_id)["languages"] == ["ar", "en", "es", "ja", "zh"]


def test_missing_weight_revision_falls_back_to_package_id_not_source_revision():
    model_id = "pocket_tts_english_safetensors"
    package = next(row for row in catalogue.inventory()["packages"] if row["id"] == model_id)
    assert package.get("weight_manifest", {}).get("revision") is None

    record = _support(model_id)
    assert record["model_revision"] == model_id
    assert record["source_revision"] == f"audio.cpp@{catalogue.inventory()['runtime_version']}"


def test_voxcpm_generations_remain_distinct_and_dialect_names_are_not_tags():
    first = _support("voxcpm1_0_5b_q8_0")
    second = _support("voxcpm2_q8_0")

    assert first["coverage"] == "exact"
    assert first["languages"] == ["en", "ja", "ko", "zh"]
    assert second["coverage"] == "subset"
    assert len(second["languages"]) == 30
    assert second["unmapped_languages"] == ["zh dialects"]
    assert "zh dialects" not in second["languages"]


def test_asr_alignment_and_voice_conversion_do_not_inherit_tts_or_language_agnostic():
    asr = catalogue.package_metadata("qwen3_asr_0_6b_q8_0")
    alignment = catalogue.package_metadata("qwen3_forced_aligner_0_6b_q8_0")
    codec = catalogue.package_metadata("miocodec_q8_0")

    assert "tts" not in asr["language_support_by_operation"]
    assert asr["language_support"]["operation"] == "asr"
    assert asr["language_support"]["coverage"] == "subset"
    assert "alignment" not in asr["language_support_by_operation"]
    assert "voice_design" not in asr["language_support_by_operation"]
    assert "tts" not in alignment["language_support_by_operation"]
    assert alignment["language_support"]["operation"] == "alignment"
    assert alignment["language_support"]["coverage"] == "exact"

    vc = codec["language_support_by_operation"]["voice_conversion"]
    s2s = codec["language_support_by_operation"]["s2s"]
    assert vc["coverage"] == "unknown" and vc["languages"] == []
    assert s2s["coverage"] == "unknown" and s2s["languages"] == []


def test_nonlinguistic_separation_is_independent_but_speech_s2s_is_unknown():
    demucs = catalogue.package_metadata("htdemucs_q8_0")
    speech_s2s = catalogue.package_metadata("personaplex_7b_v1_q8_0")

    assert demucs["language_support"]["operation"] == "sep"
    assert demucs["language_support"]["coverage"] == "independent"
    assert demucs["language_support"]["languages"] == []
    assert speech_s2s["language_support"]["operation"] == "s2s"
    assert speech_s2s["language_support"]["coverage"] == "unknown"
    assert speech_s2s["language_support"]["languages"] == []


def test_mixed_voice_conversion_family_does_not_share_tts_codes_with_other_tasks():
    metadata = catalogue.package_metadata("vevo2_q8_0")
    records = metadata["language_support_by_operation"]

    assert records["tts"]["coverage"] == "exact"
    assert records["tts"]["languages"] == ["en", "zh"]
    for operation in ("voice_conversion", "s2s", "audio_edit", "svc", "music"):
        assert records[operation]["coverage"] == "unknown"
        assert records[operation]["languages"] == []


def test_builtin_audio_utilities_keep_broad_s2s_unknown_and_mark_only_utilities_independent():
    package = {"id": "builtin-utils-fixture", "family": "builtin_audio_utils"}
    family = {
        "id": "builtin_audio_utils",
        "tasks": ["s2s"],
        "languages": ["auto"],
        "category": "audio_tools",
    }
    primary, records = catalogue._language_support(
        package, family, {"supported_languages": ["auto"]}
    )

    assert primary["operation"] == "s2s"
    assert primary["coverage"] == "unknown"
    assert records["denoise"]["coverage"] == "independent"
    assert records["denoise"]["languages"] == []
    assert records["enhance"]["coverage"] == "independent"
    assert records["enhance"]["languages"] == []


def test_catalogue_filter_uses_symmetric_locale_matching_without_crossing_regions(monkeypatch):
    row = {
        "id": "locale-fixture",
        "label": "Locale fixture",
        "family": "fixture",
        "family_label": "Fixture",
        "category": "tts",
        "description": "",
        "recommended_for": "",
        "supported_languages": ["en-gb"],
        "capabilities": [],
        "license": {"commercial_use": "unknown"},
    }
    monkeypatch.setattr(
        catalogue,
        "inventory",
        lambda: {
            "packages": [{"id": "locale-fixture"}],
            "families": [],
            "runtime_version": "test",
            "source_url": "https://example.invalid/source",
        },
    )
    monkeypatch.setattr(catalogue, "package_metadata", lambda _model_id: row)

    assert catalogue.catalogue_page(language="en", limit=10)["total"] == 1
    assert catalogue.catalogue_page(language="en-gb", limit=10)["total"] == 1
    assert catalogue.catalogue_page(language="en-us", limit=10)["total"] == 0


def test_every_advertised_language_is_a_registry_tag_and_primary_compatibility_mirror():
    registry_tags = {entry["tag"] for entry in registry_snapshot()["languages"]}
    tag_pattern = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")

    for package in catalogue.inventory()["packages"]:
        metadata = catalogue.package_metadata(package["id"])
        assert metadata["supported_languages"] == metadata["language_support"]["languages"]
        for operation, record in metadata["language_support_by_operation"].items():
            assert operation == record["operation"]
            assert record["provider_id"] == "audio_cpp"
            assert record["model_id"] == package["id"]
            assert record["native_route"].startswith("audio_cpp:")
            assert record["runtime_requirement"] == catalogue.inventory()["runtime_version"]
            for language in record["languages"]:
                assert tag_pattern.fullmatch(language)
                assert language in registry_tags
