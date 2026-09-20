"""Versioned upstream inventory and reviewed Pandrator model metadata.

Catalogue membership never implies that a model is installed or loaded. The
inventory is generated from a pinned runtime; curation records narrower claims
about exact variants and the controls implemented by Pandrator.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def inventory() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("audio_cpp_inventory.json").read_text())


@lru_cache(maxsize=1)
def curation() -> dict[str, Any]:
    return json.loads(Path(__file__).with_name("audio_cpp_curation.json").read_text())


def family_metadata(family: str) -> dict[str, Any]:
    family = {"fish_audio_s2": "fish_audio"}.get(family, family)
    found = next((row for row in inventory()["families"] if row["id"] == family), {})
    return copy.deepcopy(found)


def _curated(package: dict[str, Any]) -> dict[str, Any]:
    overrides = curation()
    result = {
        **overrides.get("families", {}).get(package["family"], {}),
        **overrides.get("models", {}).get(package["id"], {}),
    }
    model = package["id"]
    if package["family"] == "qwen3_tts":
        design, custom = "voicedesign" in model, "customvoice" in model
        result["upstream_features"] = {
            "voice_cloning": not (design or custom),
            "voice_design": design,
            "instructions": design or (custom and "1_7b" in model),
            "emotion_control": design or (custom and "1_7b" in model),
        }
        if design or custom:
            result.update(reference_audio="not_used", reference_text="not_used")
    if package["family"] == "pocket_tts":
        for name, code in {
            "english": "en",
            "german": "de",
            "italian": "it",
            "portuguese": "pt",
            "spanish": "es",
            "french": "fr",
        }.items():
            if name in model:
                result["supported_languages"] = [code]
                break
    if package["family"] == "fireredtts3":
        instruct = "instruct" in model
        result["voice_mode"] = "optional_cloning" if instruct else "cloning"
        result["reference_audio"] = "optional" if instruct else "required"
        result["upstream_features"] = {
            "voice_design": instruct,
            "instructions": instruct,
        }
    return result


def _package_availability(
    package: dict[str, Any], speech_route: bool
) -> dict[str, str]:
    available = package.get("availability") or {}
    if not speech_route:
        return {
            "status": "catalogued_only",
            "reason": "This task needs a separate generation or audio-processing workflow.",
        }
    if available.get("gated"):
        return {
            "status": "gated",
            "reason": "Upstream access terms must be accepted; automatic installation is unavailable.",
        }
    if not available.get("verified"):
        return {
            "status": "unavailable",
            "reason": "A complete package with verified file digests is not available in this snapshot.",
        }
    files = package.get("weight_manifest", {}).get("files", [])
    if (
        package.get("format") != "gguf"
        or sum(str(item.get("path", "")).lower().endswith(".gguf") for item in files)
        != 1
    ):
        return {
            "status": "catalogued_only",
            "reason": "This package layout needs a dedicated installation adapter.",
        }
    return {
        "status": "installable",
        "reason": "Available through Pandrator Manager; installation and a compatible runtime are required.",
    }


def package_metadata(model_id: str) -> dict[str, Any]:
    package = next(
        (row for row in inventory()["packages"] if row["id"] == model_id), None
    )
    if package is None:
        return {}
    family = family_metadata(package["family"])
    overrides = _curated(package)
    tasks = family.get("tasks", [])
    capabilities = family.get("capabilities") or {}
    native_features = {
        feature for values in capabilities.values() for feature in values
    }
    speech = bool(set(tasks) & {"tts", "clone", "design", "vdes"})
    # MiniMax-H3 dialogue uses the generation API rather than speech synthesis.
    speech_route = speech and package["family"] not in {"minimax_h3", "vevo2"} and not model_id.startswith("dots_tts_edit_")
    voice_mode = "cloning" if "clone" in tasks else "prebuilt"
    if not speech:
        voice_mode = "none"
    if "voicedesign" in model_id or set(tasks) <= {"design", "vdes"}:
        voice_mode = "design"
    if "customvoice" in model_id:
        voice_mode = "prebuilt"
    result = {
        "id": model_id,
        "label": package.get("label", model_id),
        "family": package["family"],
        "family_label": family.get("display_name", package["family"]),
        "category": family.get("category", "unknown"),
        "description": family.get("description", ""),
        "upstream_status": family.get("status", "unknown"),
        "tasks": tasks,
        "precision": package.get("precision"),
        "voice_mode": voice_mode,
        "supported_languages": family.get("languages", []),
        "language_note": "Upstream family coverage; quality varies by language and voice.",
        "license": {"name": "Not verified", "url": "", "commercial_use": "unknown"},
        "recommended_for": "",
        "reference_audio": "required" if voice_mode == "cloning" else "not_used",
        "reference_text": "unverified" if voice_mode == "cloning" else "not_used",
        "upstream_features": {
            "voice_cloning": "clone" in tasks,
            "voice_design": bool(set(tasks) & {"design", "vdes"}),
            "instructions": "style_control" in native_features,
            "emotion_control": "emotion_control" in native_features,
            "multi_speaker": "multi_speaker" in native_features,
            "streaming": "streaming" in family.get("modes", []),
            "speech_editing": "edit" in tasks or model_id.startswith("dots_tts_edit_"),
            "voice_conversion": "vc" in tasks,
            "sound_generation": "sfx" in tasks,
            "music_generation": "music" in tasks,
        },
        "pandrator_features": {
            "speech_generation": "request_supported"
            if speech_route
            else "catalogued_only",
            "streaming": "not_implemented",
            "speech_editing": "not_implemented",
            "audio_insertion": "not_implemented",
            "native_multi_speaker": "not_implemented",
            "acoustic_verification": "not_tested",
        },
        "package_availability": _package_availability(package, speech_route),
        "estimated_download_bytes": sum(
            file.get("size") or 0
            for file in package.get("weight_manifest", {}).get("files", [])
        )
        or None,
        "repository_license": package.get("weight_manifest", {}).get(
            "repository_license"
        ),
        "verified_runtime": inventory()["runtime_version"],
        "sources": family.get("docs") or [inventory()["source_url"]],
    }
    for key, value in overrides.items():
        if key in {"upstream_features", "pandrator_features"}:
            result[key].update(value)
        else:
            result[key] = copy.deepcopy(value)
    if not speech_route:
        result["voice_mode"] = "none"
    from .dubbing.languages import normalize_language_code

    result["supported_languages"] = list(
        dict.fromkeys(
            normalize_language_code(str(value), default="") or str(value)
            for value in result["supported_languages"]
        )
    )
    from .speech_performance import capabilities_for_model

    controls = capabilities_for_model(
        model_id,
        backend="audio_cpp",
        family=package["family"],
        voice_mode=result["voice_mode"],
        backend_version=inventory()["runtime_version"],
    )
    result["pandrator_features"].update(
        {
            "instructions": controls["instructions"],
            "voice_design": "documented" if controls["voice_design"] else "none",
            "vocal_events": ", ".join(controls["event_tags"]) or "none",
            "timing": controls["timing"],
            "emotion_control": controls["emotion"]["mode"],
        }
    )
    if result["license"].get("url"):
        result["sources"] = list(
            dict.fromkeys([result["license"]["url"], *result["sources"]])
        )
    if result["pandrator_features"]["instructions"] != "none":
        result["upstream_features"]["instructions"] = True
    if controls["event_tags"]:
        result["upstream_features"]["vocal_events"] = True
    return result


def speech_model_catalog() -> list[dict[str, Any]]:
    return [
        item
        for package in inventory()["packages"]
        if (item := package_metadata(package["id"]))["voice_mode"] != "none"
    ]


def catalogue_page(
    *,
    category: str = "",
    family: str = "",
    query: str = "",
    language: str = "",
    capability: str = "",
    commercial_use: str = "",
    recommended_only: bool = False,
    limit: int = 30,
    offset: int = 0,
) -> dict[str, Any]:
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError(
            "Catalogue limit must be 1–100 and offset must not be negative."
        )
    if commercial_use not in {
        "",
        "permitted",
        "noncommercial",
        "conditional",
        "unknown",
    }:
        raise ValueError("Unknown commercial-use filter.")
    rows = [package_metadata(package["id"]) for package in inventory()["packages"]]
    selected = []
    for row in rows:
        if category and row["category"] != category:
            continue
        if family and row["family"] != family:
            continue
        if recommended_only and not row.get("recommended_for"):
            continue
        if language:
            from .dubbing.languages import normalize_language_code

            requested = (
                normalize_language_code(language, default="") or language.casefold()
            )
            advertised = {str(item).casefold() for item in row["supported_languages"]}
            if not any(
                item == requested.casefold()
                or item.startswith(requested.casefold() + "-")
                for item in advertised
            ):
                continue
        permission = row["license"].get("commercial_use", "unknown")
        if (
            commercial_use
            and permission != commercial_use
            and not (
                commercial_use == "permitted"
                and permission == "permitted_with_attribution"
            )
        ):
            continue
        if capability and not row["upstream_features"].get(capability):
            continue
        searchable = " ".join(
            str(row.get(key, ""))
            for key in (
                "id",
                "label",
                "family_label",
                "description",
                "recommended_for",
                "supported_languages",
            )
        )
        if query and query.casefold() not in searchable.casefold():
            continue
        selected.append(row)
    selected.sort(
        key=lambda row: (not bool(row.get("recommended_for")), row["family"], row["id"])
    )
    return {
        "runtime_version": inventory()["runtime_version"],
        "source_url": inventory()["source_url"],
        "total": len(selected),
        "offset": offset,
        "limit": limit,
        "next_offset": offset + limit if offset + limit < len(selected) else None,
        "items": selected[offset : offset + limit],
        "families": [
            {
                key: value
                for key, value in item.items()
                if key in {"id", "display_name", "category", "status", "tasks"}
            }
            for item in inventory()["families"]
        ],
    }
