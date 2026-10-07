"""Provider-neutral language identifiers and static support records.

This module intentionally has no application, provider, or MCP dependencies. Its
only inputs are the adjacent, versioned JSON catalogues.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

_REGISTRY_PATH = Path(__file__).with_name("language_registry.json")
_MODEL_SOURCES_PATH = Path(__file__).with_name("model_language_sources.json")
_LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
_AUTO_ALIASES = frozenset({"auto", "automatic", "detect", "und", "unknown"})
_COVERAGE_VALUES = frozenset({"exact", "subset", "claim", "unknown", "independent"})


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not load language catalogue {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Language catalogue {path.name} must contain a JSON object")
    return value


@lru_cache(maxsize=1)
def _registry_data() -> dict[str, Any]:
    """Load and retain the registry internally; callers receive copies only."""

    return _read_json(_REGISTRY_PATH)


@lru_cache(maxsize=1)
def _model_language_sources_data() -> dict[str, Any]:
    return _read_json(_MODEL_SOURCES_PATH)


def registry_snapshot() -> dict[str, Any]:
    """Return an isolated snapshot of the versioned language registry."""

    return copy.deepcopy(_registry_data())


def _language_entries() -> list[dict[str, Any]]:
    entries = _registry_data().get("languages")
    if not isinstance(entries, list):
        raise RuntimeError("The language registry must have a languages array")
    return [entry for entry in entries if isinstance(entry, dict)]


@lru_cache(maxsize=1)
def registry_language_tags() -> frozenset[str]:
    """Share immutable registered tags for the static registry's lifetime."""

    return frozenset(
        entry["tag"] for entry in _language_entries() if isinstance(entry.get("tag"), str)
    )


@lru_cache(maxsize=1)
def _alias_index() -> dict[str, str | None]:
    index: dict[str, str | None] = {}
    for entry in _language_entries():
        tag = entry.get("tag")
        if not isinstance(tag, str):
            continue
        normalized_tag = tag.strip().casefold().replace("_", "-")
        if not _LANGUAGE_TAG_RE.fullmatch(normalized_tag):
            continue
        candidates = [entry.get("label"), *entry.get("aliases", [])]
        for alias in candidates:
            if not isinstance(alias, str):
                continue
            key = alias.strip().casefold().replace("_", "-")
            if not key:
                continue
            if key not in index:
                index[key] = normalized_tag
            elif index[key] != normalized_tag:
                # Do not silently choose an arbitrary language for a collision.
                index[key] = None
    return index


def canonical_language_tag(raw: object, *, allow_auto: bool = False) -> str:
    """Normalize a language tag or resolve an exact published registry alias."""

    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"Language tag must be text, got {type(raw).__name__}")
    value = raw.strip().casefold().replace("_", "-")
    if not value:
        return ""
    if value in _AUTO_ALIASES:
        if allow_auto:
            return "auto"
        raise ValueError(f"Automatic language value {raw!r} is not a concrete language tag")

    alias = _alias_index().get(value)
    if alias:
        return alias
    if _LANGUAGE_TAG_RE.fullmatch(value):
        return value
    raise ValueError(f"Invalid language tag or published alias: {raw!r}")


def language_matches(actual: object, requested: object) -> bool:
    """Compare canonical languages, allowing a base tag to match one locale."""

    actual_tag = canonical_language_tag(actual, allow_auto=True)
    requested_tag = canonical_language_tag(requested, allow_auto=True)
    if not actual_tag or not requested_tag or "auto" in {actual_tag, requested_tag}:
        return False
    if actual_tag == requested_tag:
        return True
    if "-" not in actual_tag and requested_tag.startswith(f"{actual_tag}-"):
        return True
    if "-" not in requested_tag and actual_tag.startswith(f"{requested_tag}-"):
        return True
    return False


def _source_records() -> Mapping[str, Any]:
    data = _model_language_sources_data()
    records = data.get("records")
    if isinstance(records, dict):
        return records
    # A direct key-to-record object is also accepted for the small static file;
    # metadata keys are excluded so they cannot be mistaken for source records.
    metadata_keys = {"schema_version", "catalogue_revision", "version"}
    return {key: value for key, value in data.items() if key not in metadata_keys}


def source_language_record(key: str) -> dict[str, Any]:
    """Return a detached raw source record, or an empty object when absent."""

    if not isinstance(key, str) or not key:
        return {}
    value = _source_records().get(key)
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _sequence(value: Sequence[str] | None, *, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a sequence of strings")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must contain only strings")
    return list(value)


def _canonical_languages(values: Sequence[str] | None) -> list[str]:
    canonical: set[str] = set()
    for value in _sequence(values, field="languages"):
        tag = canonical_language_tag(value)
        if not tag:
            raise ValueError("languages cannot contain an empty language tag")
        canonical.add(tag)
    return sorted(canonical)


def _canonical_mapping_keys(
    values: Mapping[str, str] | None, *, field: str, preserve_values: bool
) -> dict[str, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError(f"{field} must be a mapping")
    canonical: dict[str, str] = {}
    for raw_key, value in values.items():
        key = canonical_language_tag(raw_key)
        if not key:
            raise ValueError(f"{field} cannot have an empty language key")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} values must be non-empty strings")
        normalized_value = value if preserve_values else value.strip()
        if key in canonical and canonical[key] != normalized_value:
            raise ValueError(f"{field} has conflicting values for canonical language {key!r}")
        canonical[key] = normalized_value
    return dict(sorted(canonical.items()))


def _pinned_sources(source_ids: Sequence[str]) -> list[dict[str, Any]]:
    registry_sources = _registry_data().get("sources")
    if not isinstance(registry_sources, list):
        raise RuntimeError("The language registry must have a sources array")
    by_id = {
        source["id"]: source
        for source in registry_sources
        if isinstance(source, dict) and isinstance(source.get("id"), str)
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_id in source_ids:
        if source_id in seen:
            continue
        source = by_id.get(source_id)
        if source is None:
            raise ValueError(f"Unknown language source id {source_id!r}")
        seen.add(source_id)
        result.append(copy.deepcopy(source))
    return result


def _catalogue_revision() -> Any:
    registry = _registry_data()
    revision = registry.get("catalogue_revision")
    if revision is None:
        revision = registry.get("revision")
    if revision is None:
        raise RuntimeError("The language registry is missing catalogue_revision")
    return copy.deepcopy(revision)


def support_record(
    *,
    provider_id: str,
    model_id: str,
    model_revision: str,
    operation: str,
    native_route: str,
    source_key: str = "",
    languages: Sequence[str] | None = None,
    coverage: str | None = None,
    request_aliases: Mapping[str, str] | None = None,
    source_urls: Sequence[str] = (),
    runtime_requirement: str = "",
    discovery: str = "static",
    note: str = "",
) -> dict[str, Any]:
    """Build a portable, provenance-bearing support record without inference."""

    identities = {
        "provider_id": provider_id,
        "model_id": model_id,
        "model_revision": model_revision,
        "operation": operation,
        "native_route": native_route,
    }
    for field, value in identities.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
    if not isinstance(source_key, str):
        raise ValueError("source_key must be a string")

    resolved_source = source_language_record(source_key) if source_key else {}
    raw_languages = languages if languages is not None else resolved_source.get("languages")
    resolved_coverage = (
        coverage if coverage is not None else resolved_source.get("coverage", "unknown")
    )
    if resolved_coverage not in _COVERAGE_VALUES:
        raise ValueError(f"Invalid language coverage value: {resolved_coverage!r}")
    canonical_languages = _canonical_languages(raw_languages)
    if resolved_coverage == "independent":
        if languages is not None and canonical_languages:
            raise ValueError("Language-independent records must have an empty languages array")
        canonical_languages = []

    raw_aliases = (
        request_aliases
        if request_aliases is not None
        else resolved_source.get("request_aliases")
    )
    aliases = _canonical_mapping_keys(
        raw_aliases, field="request_aliases", preserve_values=True
    )

    # These are provider-native tokens, not language-tag keys. Preserve their
    # exact spelling and representation (for example, Silero's "ukr").
    native_language_codes = copy.deepcopy(resolved_source.get("native_language_codes", []))
    if not isinstance(native_language_codes, (list, dict)):
        raise ValueError(f"Source record {source_key!r} has invalid native_language_codes")

    source_ids = resolved_source.get("source_ids", [])
    if not isinstance(source_ids, list) or any(not isinstance(item, str) for item in source_ids):
        raise ValueError(f"Source record {source_key!r} has invalid source_ids")
    sources = _pinned_sources(source_ids)
    for url in _sequence(source_urls, field="source_urls"):
        if not url.strip():
            continue
        provider_source = {"url": url, "source_type": "provider_metadata"}
        if provider_source not in sources:
            sources.append(provider_source)

    record: dict[str, Any] = {
        "schema_version": 1,
        "catalogue_revision": _catalogue_revision(),
        **identities,
        "source_key": source_key,
        "source_revision": resolved_source.get("revision"),
        "coverage": resolved_coverage,
        "languages": [] if resolved_coverage == "independent" else canonical_languages,
        "native_language_codes": native_language_codes,
        "request_aliases": aliases,
        "source_ids": list(dict.fromkeys(source_ids)),
        "sources": sources,
        "runtime_requirement": runtime_requirement,
        "discovery": discovery,
        "note": note,
    }
    return record


def language_decision(record: Mapping[str, Any], requested: object) -> str:
    """Classify whether a support record establishes the requested language."""

    coverage = record.get("coverage")
    if coverage == "independent":
        return "independent"
    requested_tag = canonical_language_tag(requested, allow_auto=True)
    if not requested_tag or requested_tag == "auto":
        return "unverified"

    raw_languages = record.get("languages")
    if isinstance(raw_languages, Sequence) and not isinstance(raw_languages, str):
        for language in raw_languages:
            if isinstance(language, str) and language_matches(language, requested_tag):
                return "supported"
    if coverage == "exact":
        return "unsupported"
    return "unverified"


def require_supported_language(record: Mapping[str, Any], requested: object) -> str:
    """Return a canonical request unless an exact record proves it unsupported."""

    normalized = canonical_language_tag(requested, allow_auto=True)
    if language_decision(record, normalized) == "unsupported":
        provider = str(record.get("provider_id") or "unknown provider")
        model = str(record.get("model_id") or "unknown model")
        operation = str(record.get("operation") or "operation")
        raise ValueError(
            f"{provider} model {model} does not support {normalized!r} for {operation}"
        )
    return normalized
