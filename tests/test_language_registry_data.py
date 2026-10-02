"""Validate the versioned language data without importing application code."""

from __future__ import annotations

import json
import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOGIC_ROOT = REPOSITORY_ROOT / "pandrator" / "logic"
TAG_PATTERN = re.compile(r"^[a-z]{2,8}(?:-[a-z0-9]{1,8})*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_language_registry_is_stable_and_referentially_valid() -> None:
    registry = _load_json(LOGIC_ROOT / "language_registry.json")
    assert registry["schema_version"] == 1
    assert registry["catalogue_revision"] == "2026-10-02"

    sources = registry["sources"]
    assert isinstance(sources, list)
    source_ids = [source["id"] for source in sources]
    assert len(source_ids) == len(set(source_ids))
    assert source_ids == sorted(source_ids)
    assert all(SHA256_PATTERN.fullmatch(source["sha256"]) for source in sources)
    assert all(source["revision"] and source["retrieved_on"] for source in sources)

    entries = registry["languages"]
    assert isinstance(entries, list)
    tags = [entry["tag"] for entry in entries]
    assert tags == sorted(tags)
    assert len(tags) == len(set(tags))
    assert all(TAG_PATTERN.fullmatch(tag) for tag in tags)
    tag_set = set(tags)

    alias_owner: dict[str, str] = {}
    for entry in entries:
        aliases = entry["aliases"]
        assert aliases == sorted(aliases)
        assert len(aliases) == len(set(aliases))
        assert entry["label"]
        assert entry["source_ids"]
        assert set(entry["source_ids"]).issubset(source_ids)
        for alias in aliases:
            assert TAG_PATTERN.fullmatch(alias)
            assert alias not in tag_set
            assert alias not in alias_owner
            alias_owner[alias] = entry["tag"]

    assert alias_owner["jw"] == "jv"
    assert {"no", "nb", "nn", "tl", "fil"}.issubset(tag_set)
    assert {"sw", "my", "km", "lo"}.issubset(tag_set)
    assert {"az", "ba", "cv", "xal", "myv", "mni", "raj"}.issubset(tag_set)

    by_tag = {entry["tag"]: entry for entry in entries}
    assert by_tag["ar-eg"]["label"] == "Arabic (Egypt)"
    assert by_tag["cmn-tw"]["label"] == "Chinese, Mandarin (Taiwan)"
    assert by_tag["en-in"]["label"] == "English (India)"
    assert by_tag["es-es"]["label"] == "Spanish (Spain)"


def test_model_language_sources_preserve_counts_and_coverage_meaning() -> None:
    registry = _load_json(LOGIC_ROOT / "language_registry.json")
    model_data = _load_json(LOGIC_ROOT / "model_language_sources.json")
    assert model_data["schema_version"] == 1

    registry_source_ids = {source["id"] for source in registry["sources"]}
    model_source_ids = [source["id"] for source in model_data["sources"]]
    assert len(model_source_ids) == len(set(model_source_ids))
    assert set(model_source_ids) == registry_source_ids
    assert all(SHA256_PATTERN.fullmatch(source["sha256"]) for source in model_data["sources"])

    records = model_data["records"]
    registry_tags = {entry["tag"] for entry in registry["languages"]}
    for key, record in records.items():
        assert record["coverage"] in {"exact", "subset", "claim", "unknown"}, key
        assert record["languages"] == sorted(set(record["languages"])), key
        assert set(record["languages"]).issubset(registry_tags), key
        assert set(record["source_ids"]).issubset(registry_source_ids), key
        assert len(record["native_language_codes"]) == len(set(record["native_language_codes"])), key
        assert record["revision"], key
        assert isinstance(record["request_aliases"], dict), key

    assert records["omnivoice646"]["coverage"] == "exact"
    assert len(records["omnivoice646"]["native_language_codes"]) == 646
    assert len(records["omnivoice646"]["languages"]) == 646

    fish = records["fish_s2_pro83"]
    assert fish["coverage"] == "claim"
    assert len(fish["native_language_codes"]) == 83
    assert len(fish["languages"]) == 83
    assert "jw" in fish["native_language_codes"]
    assert fish["request_aliases"]["jv"] == "jw"

    assert records["higgs_v3_tts4b102"]["coverage"] == "exact"
    assert len(records["higgs_v3_tts4b102"]["languages"]) == 102
    assert records["openai_tts57"]["coverage"] == "exact"
    assert len(records["openai_tts57"]["languages"]) == 57
    assert records["vertex_gemini_tts87"]["coverage"] == "exact"
    assert len(records["vertex_gemini_tts87"]["languages"]) == 87
    assert records["eleven_multilingual_v2_29"]["coverage"] == "exact"
    assert len(records["eleven_multilingual_v2_29"]["languages"]) == 29

    dots = records["dots_tts24"]
    assert dots["coverage"] == "subset"
    assert len(dots["languages"]) == 24


def test_silero_records_keep_pack_scoped_codes_and_concrete_languages() -> None:
    model_data = _load_json(LOGIC_ROOT / "model_language_sources.json")
    records = model_data["records"]

    base = records["silero_v5_cis_base_nostress"]
    assert len(base["languages"]) == 20
    assert base["request_aliases"]["uk"] == "ukr"
    assert base["request_aliases"]["be"] == "bel"
    assert base["request_aliases"]["kk"] == "kaz"
    assert base["request_aliases"]["tt"] == "tat"
    assert base["request_aliases"]["uz"] == "uzb"
    assert base["request_aliases"]["cv"] == "chv"
    assert records["silero_v5_cis_base"]["languages"] == base["languages"]
    assert len(records["silero_v5_cis_ext"]["languages"]) == 6

    en_indic = records["silero_v3_en_indic"]
    assert en_indic["languages"] == ["en-in"]
    assert en_indic["native_language_codes"] == ["en-in"]
    assert en_indic["request_aliases"]["en-in"] == "en-in"

    indic = records["silero_v3_indic"]
    assert len(indic["languages"]) == 9
    assert "indic" not in indic["languages"]
    assert set(indic["languages"]) == {"bn", "gu", "hi", "kn", "ml", "mni", "raj", "ta", "te"}
