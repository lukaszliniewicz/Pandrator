"""Versioned upstream inventory and reviewed Pandrator model metadata.

Catalogue membership never implies that a model is installed or loaded. The
inventory is generated from a pinned runtime; curation records narrower claims
about exact variants and the controls implemented by Pandrator.
"""

from __future__ import annotations

import copy
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .language_capabilities import (
    canonical_language_tag,
    language_matches,
    registry_snapshot,
    support_record,
)

_LANGUAGE_CODE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
_OPERATION_BY_TASK = {
    "asr": "asr",
    "align": "alignment",
    "sep": "sep",
    "s2s": "s2s",
    "vc": "voice_conversion",
    "edit": "audio_edit",
    "denoise": "denoise",
    "enhance": "enhance",
    "music": "music",
    "sfx": "sfx",
}
_PRIMARY_OPERATION_ORDER = (
    "tts",
    "voice_design",
    "asr",
    "alignment",
    "s2s",
    "voice_conversion",
    "sep",
    "audio_edit",
    "denoise",
    "enhance",
    "music",
    "sfx",
)
_INDEPENDENT_SEPARATION_FAMILIES = frozenset(
    {"htdemucs", "htdemucs_6stems", "bs_roformer", "mel_band_roformer"}
)
_NON_LANGUAGE_SENTINELS = frozenset(
    {"auto", "automatic", "detect", "unknown", "language_agnostic", "language agnostic"}
)


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


def _package_availability(package: dict[str, Any], speech_route: bool) -> dict[str, str]:
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
        or sum(str(item.get("path", "")).lower().endswith(".gguf") for item in files) != 1
    ):
        return {
            "status": "catalogued_only",
            "reason": "This package layout needs a dedicated installation adapter.",
        }
    return {
        "status": "installable",
        "reason": "Available through Pandrator Manager; installation and a compatible runtime are required.",
    }


def _capability_notes(*values: Any) -> list[str]:
    notes: list[str] = []
    for value in values:
        if not isinstance(value, (list, tuple)):
            continue
        for item in value:
            if not isinstance(item, str):
                continue
            note = item.strip()
            normalized = note.casefold()
            if not note or any(
                marker in normalized
                for marker in ("http://", "https://", "api_key", "api key", "base_url", "secret")
            ):
                continue
            notes.append(note)
    return list(dict.fromkeys(notes))


def _language_override(package_id: str, family_id: str) -> dict[str, Any]:
    config = curation().get("language_support") or {}
    families = config.get("families") or {}
    models = config.get("models") or {}
    family_override = families.get(family_id) or {}
    model_override = models.get(package_id) or {}
    if model_override and model_override.get("operation") != family_override.get("operation"):
        family_override = {}
    return {**family_override, **model_override}


def _operation_specs(package: dict[str, Any], family: dict[str, Any]) -> list[tuple[str, str]]:
    tasks = [str(task) for task in family.get("tasks", [])]
    model_id = str(package.get("id", ""))
    if model_id.startswith("dots_tts_edit_"):
        return [("audio_edit", "edit")]

    specs: list[tuple[str, str]] = []
    tts_tasks = [task for task in ("tts", "clone", "design", "vdes") if task in tasks]
    if tts_tasks:
        # Keep the actual audio.cpp task in the native route while exposing one
        # stable operation for all speech-generation templates.
        native_task = next(
            (task for task in ("tts", "clone", "design", "vdes") if task in tasks), tts_tasks[0]
        )
        specs.append(("tts", native_task))
    design_task = next((task for task in tasks if task in {"design", "vdes"}), None)
    if design_task:
        specs.append(("voice_design", design_task))
    for task in tasks:
        if task in {"tts", "clone", "design", "vdes"}:
            continue
        operation = _OPERATION_BY_TASK.get(task, task)
        if any(existing == operation for existing, _ in specs):
            continue
        specs.append((operation, task))
    if str(package.get("family", "")) == "builtin_audio_utils" and any(
        operation == "s2s" for operation, _ in specs
    ):
        # The family exposes a broad speech-to-speech task, while its denoise
        # and enhancement utilities are language-independent sub-operations.
        specs.extend((operation, "s2s") for operation in ("denoise", "enhance"))
    if specs:
        return specs

    # A package with no declared task still needs an explicit unknown record;
    # its route cannot be inferred from a family name or provider convention.
    return [("unknown", "unknown")]


def _registry_language_tags() -> set[str]:
    return {
        str(entry["tag"])
        for entry in registry_snapshot().get("languages", [])
        if isinstance(entry, dict) and isinstance(entry.get("tag"), str)
    }


def _mapped_languages(values: Any) -> tuple[list[str], list[str], list[str], bool]:
    if values is None:
        return [], [], [], False
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, (list, tuple)):
        raw_values = list(values)
    else:
        return [], [], [], False

    tags = _registry_language_tags()
    languages: set[str] = set()
    native_codes: list[str] = []
    unmapped: list[str] = []
    concrete_data = bool(raw_values)
    for raw in raw_values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        if text.casefold() in _NON_LANGUAGE_SENTINELS:
            continue
        try:
            tag = canonical_language_tag(text)
        except ValueError:
            unmapped.append(text)
            continue
        if tag not in tags:
            unmapped.append(text)
            continue
        languages.add(tag)
        if _LANGUAGE_CODE_PATTERN.fullmatch(text):
            native_codes.append(text)
    return (
        sorted(languages),
        list(dict.fromkeys(native_codes)),
        list(dict.fromkeys(unmapped)),
        concrete_data,
    )


def _language_record(
    package: dict[str, Any],
    family: dict[str, Any],
    result: dict[str, Any],
    operation: str,
    native_task: str,
) -> dict[str, Any]:
    config = _language_override(str(package["id"]), str(package["family"]))
    config_operation = config.get("operation")
    if config_operation and config_operation != operation:
        config = {}

    source_key = str(config.get("source_key") or "")
    if source_key:
        raw_languages = config.get("languages")
        if raw_languages is None:
            from .language_capabilities import source_language_record

            raw_languages = source_language_record(source_key).get("languages", [])
    elif "languages" in config:
        raw_languages = config["languages"]
    elif operation == "tts":
        raw_languages = result.get("supported_languages", [])
    else:
        raw_languages = family.get("languages", [])

    languages, native_codes, unmapped, has_values = _mapped_languages(raw_languages)
    configured_unmapped = config.get("unmapped_languages", [])
    if isinstance(configured_unmapped, list):
        unmapped = list(dict.fromkeys([*unmapped, *(str(item) for item in configured_unmapped)]))

    explicit_coverage = config.get("coverage")
    if explicit_coverage:
        coverage = str(explicit_coverage)
    elif operation == "sep" and (
        str(package["family"]) in _INDEPENDENT_SEPARATION_FAMILIES
        or (family.get("tasks") == ["sep"] and family.get("category") == "audio_tools")
    ):
        coverage = "independent"
    elif operation in {"denoise", "enhance"} and str(package["family"]) == "builtin_audio_utils":
        coverage = "independent"
    elif operation not in {"tts", "asr", "alignment"}:
        coverage = "unknown"
    elif source_key:
        coverage = None
    elif not has_values:
        coverage = "unknown"
    elif languages and unmapped:
        coverage = "subset"
    elif languages:
        coverage = "exact"
    elif not unmapped:
        coverage = "unknown"
    else:
        coverage = "claim"

    # A generic family declaration that contains prose is never promoted to an
    # exact list. Preserve its unmapped names separately for review.
    if coverage == "exact" and unmapped:
        coverage = "subset" if languages else "claim"
    if coverage in {"unknown", "independent"}:
        languages = []

    manifest_revision = package.get("weight_manifest", {}).get("revision")
    model_revision = str(manifest_revision or package["id"])
    source_urls = family.get("docs") or [inventory()["source_url"]]
    note = str(config.get("note") or "")
    request_aliases = config.get("request_aliases")
    record = support_record(
        provider_id="audio_cpp",
        model_id=str(package["id"]),
        model_revision=model_revision,
        operation=operation,
        native_route=f"audio_cpp:{native_task}:{package['family']}",
        source_key=source_key,
        languages=languages
        if source_key and config.get("languages") is not None
        else (None if source_key else languages),
        coverage=coverage,
        request_aliases=request_aliases,
        source_urls=source_urls,
        runtime_requirement=str(inventory()["runtime_version"]),
        note=note,
    )
    if record.get("source_revision") is None:
        record["source_revision"] = f"audio.cpp@{inventory()['runtime_version']}"
    if not source_key:
        configured_native = config.get("native_language_codes")
        record["native_language_codes"] = (
            list(configured_native) if isinstance(configured_native, list) else native_codes
        )
    if record["coverage"] in {"unknown", "independent"}:
        record["languages"] = []
        record["native_language_codes"] = []
    if unmapped:
        record["unmapped_languages"] = unmapped
    return record


def _language_support(
    package: dict[str, Any], family: dict[str, Any], result: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    records: dict[str, dict[str, Any]] = {}
    for operation, native_task in _operation_specs(package, family):
        if operation == "voice_design" and "tts" in records:
            # The package's TTS record already carries its exact package-level
            # evidence binding. Reuse that evidence for the same model's
            # native design task, while keeping a distinct operation and route.
            record = copy.deepcopy(records["tts"])
            record["operation"] = operation
            record["native_route"] = f"audio_cpp:{native_task}:{package['family']}"
            records[operation] = record
        else:
            records[operation] = _language_record(package, family, result, operation, native_task)
    primary_operation = next(
        (operation for operation in _PRIMARY_OPERATION_ORDER if operation in records),
        next(iter(records)),
    )
    return records[primary_operation], records


def package_metadata(model_id: str) -> dict[str, Any]:
    package = next((row for row in inventory()["packages"] if row["id"] == model_id), None)
    if package is None:
        return {}
    family = family_metadata(package["family"])
    overrides = _curated(package)
    tasks = family.get("tasks", [])
    capabilities = family.get("capabilities") or {}
    native_features = {feature for values in capabilities.values() for feature in values}
    speech = bool(set(tasks) & {"tts", "clone", "design", "vdes"})
    # MiniMax-H3 dialogue uses the generation API rather than speech synthesis.
    speech_route = (
        speech
        and package["family"] not in {"minimax_h3", "vevo2"}
        and not model_id.startswith("dots_tts_edit_")
    )
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
            "speech_generation": "request_supported" if speech_route else "catalogued_only",
            "streaming": "not_implemented",
            "speech_editing": "not_implemented",
            "audio_insertion": "not_implemented",
            "native_multi_speaker": "not_implemented",
            "acoustic_verification": "not_tested",
        },
        "package_availability": _package_availability(package, speech_route),
        "estimated_download_bytes": sum(
            file.get("size") or 0 for file in package.get("weight_manifest", {}).get("files", [])
        )
        or None,
        "repository_license": package.get("weight_manifest", {}).get("repository_license"),
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
    primary_language_support, operation_language_support = _language_support(
        package, family, result
    )
    result["language_support"] = primary_language_support
    result["language_support_by_operation"] = operation_language_support
    result["supported_languages"] = copy.deepcopy(primary_language_support["languages"])
    from .speech_capabilities import capabilities_for_model

    controls = capabilities_for_model(
        model_id,
        backend="audio_cpp",
        family=package["family"],
        voice_mode=result["voice_mode"],
        backend_version=inventory()["runtime_version"],
    )
    instruction_scope = controls.get("instruction_scope")
    instruction_scope_text = (
        ", ".join(str(scope).strip() for scope in instruction_scope if str(scope).strip())
        if isinstance(instruction_scope, (list, tuple))
        else ""
    )
    capability_notes = _capability_notes(
        controls.get("notes"),
        result["pandrator_features"].get("capability_notes"),
    )
    if package["family"] == "fireredtts3" and "instruct" in model_id.casefold():
        capability_notes = _capability_notes(
            capability_notes,
            [
                "FireRed Instruct supports instructions, emotion control, and voice design only without reference audio; clone mode uses the instruction field as the reference transcript."
            ],
        )
    result["pandrator_features"].update(
        {
            "instructions": controls["instructions"],
            "instruction_scope": instruction_scope_text or "none",
            "voice_design": "documented" if controls["voice_design"] else "none",
            "vocal_events": ", ".join(controls["event_tags"]) or "none",
            "timing": controls["timing"],
            "emotion_control": controls["emotion"]["mode"],
            "semantic_context": str(controls.get("semantic_context") or "none"),
            "capability_notes": capability_notes,
        }
    )
    # Keep documented upstream task flags; derive runtime controls separately.
    normalized_capabilities = {
        feature
        for feature, enabled in result["upstream_features"].items()
        if enabled is True
        and feature
        not in {
            "voice_cloning",
            "voice_design",
            "instructions",
            "emotion_control",
            "vocal_events",
        }
    }
    if (
        result["voice_mode"] in {"cloning", "optional_cloning", "hybrid"}
        and result["upstream_features"].get("voice_cloning") is True
    ):
        normalized_capabilities.add("voice_cloning")
    if result["voice_mode"] in {"prebuilt", "hybrid"}:
        normalized_capabilities.add("prebuilt_voices")
    if controls["voice_design"]:
        normalized_capabilities.add("voice_design")
    if controls["instructions"] != "none":
        normalized_capabilities.add("instructions")
    if controls["emotion"]["mode"] != "none":
        normalized_capabilities.add("emotion_control")
    if controls["event_tags"]:
        normalized_capabilities.add("vocal_events")
    # Preserve the Manager's legacy tag only when multiple languages are listed.
    if len(result["supported_languages"]) > 1:
        normalized_capabilities.add("multilingual")
    result["capabilities"] = sorted(normalized_capabilities)
    if result["license"].get("url"):
        result["sources"] = list(dict.fromkeys([result["license"]["url"], *result["sources"]]))
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
        raise ValueError("Catalogue limit must be 1–100 and offset must not be negative.")
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
    normalized_capability = {"emotions": "emotion_control"}.get(capability, capability)
    try:
        requested_language = canonical_language_tag(language) if language else ""
    except ValueError:
        requested_language = ""
    for row in rows:
        if category and row["category"] != category:
            continue
        if family and row["family"] != family:
            continue
        if recommended_only and not row.get("recommended_for"):
            continue
        if language:
            if not requested_language or not any(
                language_matches(item, requested_language) for item in row["supported_languages"]
            ):
                continue
        permission = row["license"].get("commercial_use", "unknown")
        if (
            commercial_use
            and permission != commercial_use
            and not (commercial_use == "permitted" and permission == "permitted_with_attribution")
        ):
            continue
        if normalized_capability and normalized_capability not in row["capabilities"]:
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
    selected.sort(key=lambda row: (not bool(row.get("recommended_for")), row["family"], row["id"]))
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
