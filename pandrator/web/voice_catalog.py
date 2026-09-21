"""One bounded, evidence-aware voice inventory for the UI and MCP."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .voice_catalog_schemas import VoiceProfile, VoiceReference, canonical_voice_key


class VoiceCatalogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(default="", max_length=300)
    language: str = Field(default="", max_length=40)
    accent: str = Field(default="", max_length=80)
    voice_category: Literal["", "male", "female", "androgynous", "unspecified"] = ""
    pitch: Literal["", "low", "mid", "high"] = ""
    perceived_age: Literal["", "childlike", "youthful", "adult", "older"] = ""
    texture: str = Field(default="", max_length=40)
    delivery_preset: str = Field(default="", max_length=40)
    tag: str = Field(default="", max_length=40)
    use_case: str = Field(default="", max_length=40)
    collection_id: str = Field(default="", max_length=160)
    kind: Literal["all", "managed", "provider"] = "all"
    origin: str = Field(default="", max_length=40)
    service_id: str = Field(default="", max_length=160)
    model: str = Field(default="", max_length=200)
    ready_only: bool = False
    reviewed_only: bool = False
    sort: Literal["relevance", "name", "recently_added", "recently_updated"] = (
        "relevance"
    )
    limit: int = Field(default=30, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=1024)


TAXONOMY = {
    "voice_category": ["male", "female", "androgynous", "unspecified"],
    "pitch": ["low", "mid", "high"],
    "perceived_age": ["childlike", "youthful", "adult", "older"],
    "textures": [
        "warm",
        "bright",
        "dark",
        "airy",
        "breathy",
        "raspy",
        "gravelly",
        "resonant",
        "clear",
        "nasal",
    ],
    "delivery_presets": [
        "neutral",
        "conversational",
        "formal",
        "storytelling",
        "dramatic",
    ],
    "use_cases": [
        "audiobook_narration",
        "character_dialogue",
        "voiceover",
        "documentary",
        "news",
        "advertising",
        "instructional",
    ],
}

# Descriptive evidence from the published Qwen CustomVoice speaker table.
# These are native profiles, not a restriction on synthesis language.
_QWEN = {
    "aiden": ("male", "en", "American", "Clear, sunny midrange", ["clear", "bright"]),
    "ryan": ("male", "en", "", "Dynamic voice with a strong rhythmic drive", []),
    "dylan": ("male", "zh", "Beijing", "Youthful, clear voice", ["clear"]),
    "eric": ("male", "zh", "Sichuan", "Lively, slightly husky brightness", ["bright"]),
    "ono_anna": ("female", "ja", "", "Light, nimble and playful", []),
    "serena": ("female", "zh", "", "Warm and gentle", ["warm"]),
    "sohee": ("female", "ko", "", "Warm and expressive", ["warm"]),
    "uncle_fu": ("male", "zh", "", "Low, mellow and seasoned", ["dark", "resonant"]),
    "vivian": ("female", "zh", "", "Bright and slightly edgy", ["bright"]),
}
_EVIDENCE = {"source": "provider", "status": "described"}


def normalized_profile(raw: Any) -> dict[str, Any]:
    try:
        return VoiceProfile.model_validate(raw or {}).model_dump(mode="json")
    except (ValidationError, TypeError):
        # Legacy free-form metadata must not make the entire catalog unreadable.
        return VoiceProfile().model_dump(mode="json")


def _language(value: Any) -> str:
    return str(value or "").strip().replace("_", "-").lower()


def _language_matches(actual: str, requested: str) -> bool:
    actual, requested = _language(actual), _language(requested)
    return actual == requested or (
        "-" not in requested and actual.split("-")[0] == requested
    )


def _category(value: Any) -> str:
    value = str(value or "").strip().casefold()
    return value if value in TAXONOMY["voice_category"] else "unspecified"


def listed_models(service: dict[str, Any]) -> list[str]:
    return [
        str(m.get("id") if isinstance(m, dict) else m)
        for m in service.get("models", [])
    ]


def catalog_models(service: dict[str, Any]) -> list[dict[str, Any]]:
    metadata = {
        str(m.get("id")): dict(m)
        for m in service.get("model_catalog", [])
        if isinstance(m, dict) and m.get("id")
    }
    listed = [
        str(m.get("id") if isinstance(m, dict) else m)
        for m in service.get("models", [])
    ]
    if service.get("default_model"):
        listed.append(str(service["default_model"]))
    # Include known-but-not-ready models, exposing their state separately.
    return [
        {**metadata.get(model, {}), "id": model}
        for model in dict.fromkeys([*listed, *metadata])
        if model
    ]


def model_modes(service: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    identifier = str(model["id"])
    expressive = (
        model.get("expressive_capabilities")
        or (service.get("expressive_capabilities") or {}).get(identifier)
        or {}
    )
    mode = model.get("voice_mode") or (service.get("model_voice_modes") or {}).get(
        identifier
    )
    if not mode:
        mode = (
            ("hybrid" if service.get("voices") else "cloning")
            if service.get("supports_voice_cloning")
            else "prebuilt"
        )
    cloning = mode in {"cloning", "hybrid", "optional_cloning"}
    design = mode in {"design", "optional_cloning"} or bool(
        expressive.get("voice_design")
    )
    instructions = expressive.get("instructions", "none")
    known = expressive.get("status") in {"documented", "verified"}
    return {
        "voice_mode": mode,
        "prebuilt": mode in {"prebuilt", "hybrid"},
        "cloning": cloning,
        "design": design,
        "reference_with_instructions": cloning and known and instructions != "none",
        "instructions": instructions,
        "instruction_scope": expressive.get("instruction_scope", []),
        "vocal_events": sorted((expressive.get("event_tags") or {}).keys()),
        "environmental_effects": False,
        "evidence_status": expressive.get("status", "unknown"),
        "reference": {
            "format": "managed_audio",
            "reviewed_transcript_recommended": True,
        }
        if cloning
        else None,
        "reuse": "save_reference_then_clone"
        if design
        else "provider_speaker"
        if mode == "prebuilt"
        else "reviewed_reference",
    }


def supported_languages(service: dict[str, Any], model: dict[str, Any]) -> list[str]:
    values = (
        model.get("languages")
        or model.get("supported_languages")
        or service.get("languages")
        or service.get("supported_languages")
        or []
    )
    if not values and "qwen3_tts" in str(model.get("id")):
        values = ["zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"]
    if not values and "breeze_tts" in str(model.get("id")):
        values = ["zh", "en"]
    return list(
        dict.fromkeys(
            _language(v.get("id") or v.get("code") or v.get("language_id"))
            if isinstance(v, dict)
            else _language(v)
            for v in values
            if v
        )
    )


def _provider_profile(
    service: dict[str, Any], model: dict[str, Any], voice: str, metadata: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    profile = normalized_profile(metadata.get("profile"))
    raw_labels = metadata.get("labels")
    labels = raw_labels if isinstance(raw_labels, dict) else {}
    category = _category(metadata.get("gender") or labels.get("gender"))
    description = str(metadata.get("description") or "")
    family = str(model.get("family") or model.get("id") or "").lower()
    known = _QWEN.get(voice.lower()) if "qwen" in family else None
    if known:
        category, lang, accent, description, textures = known
        profile.update(
            textures=textures,
            languages=[
                {
                    "language": lang,
                    "locale": None,
                    "accent": accent or None,
                    "detail": None,
                    "evidence": dict(_EVIDENCE),
                }
            ],
        )
        profile["evidence"]["textures"] = dict(_EVIDENCE)
        profile["evidence"]["voice_category"] = dict(_EVIDENCE)
    else:
        locale = _language(metadata.get("locale") or metadata.get("language_code"))
        # A provider's human-readable language label is not a language code.
        language = locale.split("-")[0] or _language(labels.get("language"))
        if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", language):
            language = ""
        if not language and re.fullmatch(
            r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*",
            str(metadata.get("language") or ""),
            re.IGNORECASE,
        ):
            language = _language(metadata["language"])
        if language:
            profile["languages"] = [
                {
                    "language": language,
                    "locale": locale or None,
                    "accent": str(metadata.get("accent") or labels.get("accent") or "")
                    or None,
                    "detail": None,
                    "evidence": dict(_EVIDENCE),
                }
            ]
        if category != "unspecified":
            profile["evidence"]["voice_category"] = dict(_EVIDENCE)
    return category, description, profile


def catalog_entries(
    managed: list[dict[str, Any]],
    catalog: dict[str, Any],
    collections: list[dict[str, Any]],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Normalize saved and provider voices without exposing provider secrets."""
    services = catalog.get("services") or []
    memberships: dict[str, list[dict[str, str]]] = {}
    for collection in collections:
        for member in collection.get("members", []):
            memberships.setdefault(member["key"], []).append(
                {"id": collection["id"], "name": collection["name"]}
            )
    entries = []
    for voice in managed:
        ref = VoiceReference(kind="managed", voice_id=voice["id"])
        key = canonical_voice_key(ref)
        metadata = voice.get("metadata_json") or {}
        profile = normalized_profile(voice.get("profile") or metadata.get("profile"))
        if not profile["languages"] and voice.get("language"):
            profile["languages"] = [
                {
                    "language": _language(voice["language"]),
                    "locale": None,
                    "accent": None,
                    "detail": None,
                    "evidence": {"source": "user", "status": "described"},
                }
            ]
        compatibility = []
        for service in services:
            registration = (metadata.get("providers") or {}).get(service["id"]) or {}
            for model in catalog_models(service):
                modes = model_modes(service, model)
                if not modes["cloning"]:
                    continue
                status = (
                    "needs_reference"
                    if not voice.get("available_sample_count")
                    else "needs_link"
                    if registration.get("status") != "ready"
                    else "unavailable"
                    if service.get("available") is False
                    else "ready"
                    if service.get("available") is True
                    else "unknown"
                )
                if status == "ready" and model["id"] not in listed_models(service):
                    status = "model_unavailable"
                compatibility.append(
                    {
                        "service_id": service["id"],
                        "model": model["id"],
                        "status": status,
                        "ready": status == "ready",
                        "voice": registration.get("voice_id"),
                        "modes": modes,
                        "supported_languages": supported_languages(service, model),
                    }
                )
        entries.append(
            {
                "key": key,
                "reference": ref.model_dump(exclude_none=True),
                "id": voice["id"],
                "kind": "managed",
                "name": voice["name"],
                "description": voice.get("description") or "",
                "language": voice.get("language"),
                "voice_category": _category(
                    voice.get("voice_category") or metadata.get("voice_category")
                ),
                "profile": profile,
                "origin": voice.get("origin")
                or (
                    "builtin"
                    if voice.get("bundled")
                    else "imported"
                    if voice.get("sample_count")
                    else "unknown"
                ),
                "revision": voice["revision"],
                "collections": memberships.get(key, []),
                "compatibility": compatibility,
                "preview_artifact_id": voice.get("preview_artifact_id"),
                "sample_count": voice.get("sample_count", 0),
                "created_at": voice.get("created_at"),
                "updated_at": voice.get("updated_at"),
                "bundled": bool(voice.get("bundled")),
            }
        )
    for service in services:
        for model in catalog_models(service):
            modes = model_modes(service, model)
            if not modes["prebuilt"] and not modes["cloning"]:
                continue
            catalogues = service.get("voice_catalogues") or {}
            raw = catalogues.get(model["id"])
            if raw is None:
                raw = (
                    service.get("voices") or []
                    if not catalogues or model["id"] == service.get("default_model")
                    else []
                )
            if isinstance(raw, dict):
                raw = [
                    {"id": k, **(v if isinstance(v, dict) else {})}
                    for k, v in raw.items()
                ]
            seen = set()
            for record in raw:
                metadata = record if isinstance(record, dict) else {}
                voice = str(
                    metadata.get("id")
                    or metadata.get("voice_id")
                    or metadata.get("name")
                    or record
                )
                if not voice or voice in seen:
                    continue
                seen.add(voice)
                indexed_metadata = service.get("voice_metadata") or {}
                extra = indexed_metadata.get(
                    f"{model['id']}:{voice}"
                ) or indexed_metadata.get(voice)
                if isinstance(extra, dict):
                    metadata = {**extra, **metadata}
                ref = VoiceReference(
                    kind="provider",
                    service_id=service["id"],
                    model=model["id"],
                    voice=voice,
                )
                key = canonical_voice_key(ref)
                category, description, profile = _provider_profile(
                    service, model, voice, metadata
                )
                override = (overrides or {}).get(key) or {}
                if override.get("profile") is not None:
                    profile = normalized_profile(override["profile"])
                status = (
                    "ready"
                    if service.get("available") is True
                    and model["id"] in listed_models(service)
                    else "unavailable"
                    if service.get("available") is False
                    else "unknown"
                )
                preview = next(
                    (
                        p.get("artifact_id")
                        for p in catalog.get("previews", [])
                        if p.get("service_id") == service["id"]
                        and p.get("model") == model["id"]
                        and p.get("voice") == voice
                    ),
                    None,
                )
                entries.append(
                    {
                        "key": key,
                        "reference": ref.model_dump(exclude_none=True),
                        "id": voice,
                        "kind": "provider",
                        "name": override.get("name")
                        or metadata.get("display_name")
                        or metadata.get("name")
                        or voice,
                        "description": override.get("description")
                        if override.get("description") is not None
                        else description,
                        "voice_category": override.get("voice_category") or category,
                        "profile": profile,
                        "origin": "builtin" if modes["prebuilt"] else "unknown",
                        "revision": override.get("revision", 0),
                        "collections": memberships.get(key, []),
                        "compatibility": [
                            {
                                "service_id": service["id"],
                                "model": model["id"],
                                "status": status,
                                "ready": status == "ready",
                                "voice": voice,
                                "modes": modes,
                                "supported_languages": supported_languages(
                                    service, model
                                ),
                            }
                        ],
                        "preview_artifact_id": preview,
                        "created_at": override.get("created_at"),
                        "updated_at": override.get("updated_at"),
                        "bundled": False,
                    }
                )
    return entries


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode()
    ).hexdigest()[:24]


def query_catalog(
    entries: list[dict[str, Any]], query: VoiceCatalogQuery
) -> dict[str, Any]:
    params = query.model_dump(exclude={"cursor", "limit"})
    fingerprint = _digest(params)
    revision = _digest(entries)
    offset = 0
    if query.cursor:
        try:
            cursor = json.loads(
                base64.urlsafe_b64decode(query.cursor.encode()).decode()
            )
            if cursor["query"] != fingerprint or cursor["revision"] != revision:
                raise ValueError("The catalog or filters changed. Restart the search.")
            offset = cursor["offset"]
            if type(offset) is not int or offset < 0:
                raise ValueError("Invalid catalog cursor.")
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeError) as error:
            raise ValueError("Invalid catalog cursor.") from error
    tokens = query.query.casefold().split()
    results = []
    for original in entries:
        item = deepcopy(original)
        profile = item["profile"]
        if query.kind != "all" and item["kind"] != query.kind:
            continue
        if query.collection_id and not any(
            c["id"] == query.collection_id for c in item["collections"]
        ):
            continue
        if query.origin and item["origin"] != query.origin:
            continue
        if query.voice_category and item["voice_category"] != query.voice_category:
            continue
        if query.pitch and profile.get("pitch") != query.pitch:
            continue
        if query.perceived_age and profile.get("perceived_age") != query.perceived_age:
            continue
        if query.texture and query.texture not in profile["textures"]:
            continue
        if query.delivery_preset and query.delivery_preset not in profile["delivery_presets"]:
            continue
        if query.tag and not any(
            query.tag.casefold() == tag.casefold() for tag in profile["tags"]
        ):
            continue
        if query.use_case and query.use_case not in profile["use_cases"]:
            continue
        linguistic = [
            lang
            for lang in profile["languages"]
            if (
                not query.language
                or _language_matches(lang["language"], query.language)
            )
            and (
                not query.accent
                or query.accent.casefold() in str(lang.get("accent") or "").casefold()
            )
            and (lang.get("evidence") or {}).get("status") != "requested"
            and (
                not query.reviewed_only
                or (lang.get("evidence") or {}).get("status") == "reviewed"
            )
        ]
        if query.accent and not linguistic:
            continue
        if any(
            value
            and (
                (profile.get("evidence", {}).get(field) or {}).get("status")
                == "requested"
                or (
                    query.reviewed_only
                    and (profile.get("evidence", {}).get(field) or {}).get("status")
                    != "reviewed"
                )
            )
            for field, value in [
                ("voice_category", query.voice_category),
                ("pitch", query.pitch),
                ("perceived_age", query.perceived_age),
                ("textures", query.texture),
                ("delivery_presets", query.delivery_preset),
                ("tags", query.tag),
                ("use_cases", query.use_case),
            ]
        ):
            continue
        compatibility = [
            c
            for c in item["compatibility"]
            if (not query.service_id or c["service_id"] == query.service_id)
            and (not query.model or c["model"] == query.model)
        ]
        if query.language:
            compatibility = [
                c
                for c in compatibility
                if not c.get("supported_languages")
                or any(
                    _language_matches(lang, query.language)
                    for lang in c["supported_languages"]
                )
            ]
            supported = any(
                any(
                    _language_matches(lang, query.language)
                    for lang in c.get("supported_languages", [])
                )
                for c in compatibility
            )
            if not linguistic and (query.reviewed_only or not supported):
                continue
        if (query.service_id or query.model) and not compatibility:
            continue
        if query.ready_only and not any(c["ready"] for c in compatibility):
            continue
        searchable = " ".join(
            [
                item["id"],
                item["name"],
                item["description"],
                item["voice_category"],
                str(profile.get("pitch") or ""),
                str(profile.get("perceived_age") or ""),
                *profile["textures"],
                *profile["delivery_presets"],
                *profile["use_cases"],
                *profile["tags"],
                *(
                    " ".join(
                        str(lang.get(k) or "")
                        for k in ("language", "locale", "accent", "detail")
                    )
                    for lang in profile["languages"]
                ),
            ]
        ).casefold()
        if not all(token in searchable for token in tokens):
            continue
        if query.reviewed_only and not any(
            (e or {}).get("status") == "reviewed"
            for e in [
                *profile.get("evidence", {}).values(),
                *(lang.get("evidence") for lang in profile["languages"]),
            ]
        ):
            continue
        item["compatibility"] = compatibility
        item["match_reasons"] = [
            f"{label}: {value}"
            for label, value in [
                ("Language", query.language),
                ("Accent", query.accent),
                ("Presentation", query.voice_category),
                ("Pitch", query.pitch),
                ("Perceived age", query.perceived_age),
                ("Texture", query.texture),
                ("Delivery", query.delivery_preset),
                ("Tag", query.tag),
                ("Use", query.use_case),
            ]
            if value
        ]
        item["match_reasons"] += (
            ["Reviewed trait evidence"] if query.reviewed_only else []
        )
        if query.language and not linguistic:
            item["match_reasons"].append(
                "Language supported by renderer; this voice's accent is unreviewed"
            )
        results.append(item)
    results.sort(key=lambda i: (i["name"].casefold(), i["key"]))
    if query.sort in {"recently_added", "recently_updated"}:
        field = "created_at" if query.sort == "recently_added" else "updated_at"
        results.sort(key=lambda i: i.get(field) or "", reverse=True)
    elif query.sort == "relevance" and tokens:
        results.sort(
            key=lambda i: sum(t in i["name"].casefold() for t in tokens), reverse=True
        )
    page = results[offset : offset + query.limit]
    next_offset = offset + len(page)
    cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"query": fingerprint, "revision": revision, "offset": next_offset}
            ).encode()
        ).decode()
        if next_offset < len(results)
        else None
    )
    facets = {
        name: dict(Counter(str(i.get(name) or "unknown") for i in entries))
        for name in ("voice_category", "origin", "kind")
    }
    facets["language"] = dict(
        Counter(lang["language"] for i in entries for lang in i["profile"]["languages"])
    )
    return {
        "schema_version": "1",
        "catalog_revision": revision,
        "items": page,
        "total": len(results),
        "next_cursor": cursor,
        "facets": facets,
        "taxonomy": TAXONOMY,
    }
