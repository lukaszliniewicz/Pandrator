import concurrent.futures
import json
import re
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import requests


DISCOVERY_TIMEOUT_SECONDS = 4
OPENAI_ADAPTER = "openai_compatible"
GENERIC_JSON_ADAPTER = "generic_json"

COMMON_SPEECH_PATHS = (
    "/v1/audio/speech",
    "/audio/speech",
    "/tts/generate",
    "/api/tts",
    "/synthesize",
    "/tts",
    "/generate",
)
COMMON_MODEL_PATHS = (
    "/v1/models",
    "/models",
    "/v1/audio/models",
    "/audio/models",
)
COMMON_VOICE_PATHS = (
    "/v1/audio/voices",
    "/audio/voices",
    "/v1/voices",
    "/voices",
)

TEXT_FIELD_ALIASES = ("input", "text", "prompt", "sentence", "content")
MODEL_FIELD_ALIASES = ("model", "model_id", "model_name")
VOICE_FIELD_ALIASES = ("voice", "voice_id", "speaker", "speaker_id", "speaker_name")
SPEED_FIELD_ALIASES = ("speed", "rate", "speaking_rate")
FORMAT_FIELD_ALIASES = ("response_format", "format", "audio_format", "output_format")


def normalize_endpoint_base_url(raw_url: str) -> str:
    normalized = str(raw_url or "").strip()
    if not normalized:
        return ""
    if "://" not in normalized:
        normalized = f"http://{normalized}"
    return normalized.rstrip("/")


def _url_for_path(base_url: str, path: str) -> str:
    normalized_path = f"/{str(path or '').lstrip('/')}"
    parsed = urlparse(base_url)
    origin = urlunparse(parsed._replace(path="", params="", query="", fragment="")).rstrip("/")
    return urljoin(f"{origin}/", normalized_path.lstrip("/"))


def _openapi_candidate_urls(base_url: str) -> list[str]:
    parsed = urlparse(base_url)
    parent_path = parsed.path.rstrip("/")
    if parent_path.lower().endswith("/v1"):
        parent_path = parent_path[:-3]
    parent = urlunparse(parsed._replace(path=parent_path or "", params="", query="", fragment="")).rstrip("/")

    candidates = [
        _url_for_path(base_url, "/openapi.json"),
        _url_for_path(base_url, "/v1/openapi.json"),
    ]
    if parent and parent != base_url:
        candidates.append(_url_for_path(parent, "/openapi.json"))
    return _dedupe(candidates)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _auth_headers(api_key: str) -> dict[str, str]:
    normalized_key = str(api_key or "").strip()
    if not normalized_key:
        return {}
    return {"Authorization": f"Bearer {normalized_key}"}


def _safe_get(url: str, api_key: str) -> dict[str, Any]:
    try:
        response = requests.get(
            url,
            headers=_auth_headers(api_key),
            timeout=DISCOVERY_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as exc:
        return {"url": url, "status": 0, "error": str(exc), "payload": None}

    payload = None
    content_type = str(response.headers.get("Content-Type") or "").lower()
    if "json" in content_type or response.text.lstrip().startswith(("{", "[")):
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            payload = None

    return {
        "url": url,
        "status": int(response.status_code),
        "content_type": content_type,
        "payload": payload,
        "text": response.text[:500],
    }


def _probe_urls(urls: list[str], api_key: str) -> dict[str, dict[str, Any]]:
    if not urls:
        return {}
    max_workers = min(12, len(urls))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(lambda url: _safe_get(url, api_key), urls)
    return {result["url"]: result for result in results}


def _resolve_schema(schema: Any, openapi: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return {}

    ref = str(schema.get("$ref") or "")
    if ref.startswith("#/"):
        current: Any = openapi
        for part in ref[2:].split("/"):
            if not isinstance(current, dict):
                return {}
            current = current.get(part)
        return _resolve_schema(current, openapi)

    combined: dict[str, Any] = dict(schema)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for composition_key in ("allOf", "anyOf", "oneOf"):
        variants = schema.get(composition_key)
        if not isinstance(variants, list):
            continue
        for variant in variants:
            resolved = _resolve_schema(variant, openapi)
            properties.update(resolved.get("properties", {}))
            required.extend(resolved.get("required", []))

    properties.update(schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {})
    if properties:
        combined["properties"] = properties
    if required or isinstance(schema.get("required"), list):
        combined["required"] = _dedupe(required + list(schema.get("required") or []))
    return combined


def _operation_json_schema(operation: dict[str, Any], openapi: dict[str, Any]) -> dict[str, Any]:
    request_body = _resolve_schema(operation.get("requestBody", {}), openapi)
    if not isinstance(request_body, dict):
        request_body = {}
    content = request_body.get("content", {})
    if isinstance(content, dict):
        for content_type in ("application/json", "application/*+json"):
            entry = content.get(content_type)
            if isinstance(entry, dict):
                return _resolve_schema(entry.get("schema", {}), openapi)

        for content_type, entry in content.items():
            if "json" in str(content_type).lower() and isinstance(entry, dict):
                return _resolve_schema(entry.get("schema", {}), openapi)

    parameters = operation.get("parameters", [])
    if isinstance(parameters, list):
        for parameter in parameters:
            resolved = _resolve_schema(parameter, openapi)
            if resolved.get("in") == "body":
                return _resolve_schema(resolved.get("schema", {}), openapi)
    return {}


def _matching_field(properties: dict[str, Any], aliases: tuple[str, ...]) -> str:
    by_lower = {str(name).lower(): str(name) for name in properties}
    for alias in aliases:
        if alias in by_lower:
            return by_lower[alias]
    return ""


def _schema_enum(property_schema: Any, openapi: dict[str, Any]) -> list[str]:
    resolved = _resolve_schema(property_schema, openapi)
    enum_values = resolved.get("enum", [])
    if not isinstance(enum_values, list):
        return []
    return _dedupe([str(value) for value in enum_values])


def _schema_defaults(
    properties: dict[str, Any],
    openapi: dict[str, Any],
    excluded_fields: set[str],
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for field_name, property_schema in properties.items():
        if field_name in excluded_fields:
            continue
        resolved = _resolve_schema(property_schema, openapi)
        if "default" not in resolved:
            continue
        default = resolved["default"]
        if isinstance(default, (str, int, float, bool)) or default is None:
            defaults[field_name] = default
    return defaults


def _response_returns_audio(operation: dict[str, Any]) -> bool:
    produces = operation.get("produces", [])
    if isinstance(produces, list) and any(
        "audio" in str(content_type).lower()
        or any(token in str(content_type).lower() for token in ("mpeg", "wav", "ogg", "flac"))
        for content_type in produces
    ):
        return True

    responses = operation.get("responses", {})
    if not isinstance(responses, dict):
        return False
    for response in responses.values():
        if not isinstance(response, dict):
            continue
        content = response.get("content", {})
        if not isinstance(content, dict):
            continue
        if any(
            "audio" in str(content_type).lower()
            or any(token in str(content_type).lower() for token in ("mpeg", "wav", "ogg", "flac"))
            for content_type in content
        ):
            return True
    return False


def _speech_path_score(path: str, operation: dict[str, Any], schema: dict[str, Any]) -> int:
    lowered_path = path.lower()
    score = 0
    if lowered_path.endswith("/v1/audio/speech") or lowered_path.endswith("/audio/speech"):
        score += 100
    elif "speech" in lowered_path or "synthesize" in lowered_path:
        score += 75
    elif "tts" in lowered_path:
        score += 65
    elif "generate" in lowered_path:
        score += 50

    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    if _matching_field(properties, TEXT_FIELD_ALIASES):
        score += 40
    if _matching_field(properties, VOICE_FIELD_ALIASES):
        score += 10
    if _response_returns_audio(operation):
        score += 35
    return score


def _find_openapi_catalog_path(openapi: dict[str, Any], kind: str) -> str:
    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        return ""
    token = "model" if kind == "models" else "voice"
    candidates: list[tuple[int, str]] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict) or not isinstance(path_item.get("get"), dict):
            continue
        lowered = str(path).lower()
        if token not in lowered:
            continue
        score = 20
        if lowered.endswith(f"/{kind}"):
            score += 30
        if "audio" in lowered and kind == "voices":
            score += 15
        candidates.append((score, str(path)))
    if not candidates:
        return ""
    return max(candidates)[1]


def _analyze_openapi(openapi: dict[str, Any]) -> dict[str, Any] | None:
    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        return None

    candidates: list[tuple[int, str, dict[str, Any], dict[str, Any]]] = []
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        operation = path_item.get("post")
        if not isinstance(operation, dict):
            continue
        schema = _operation_json_schema(operation, openapi)
        score = _speech_path_score(str(path), operation, schema)
        if score:
            candidates.append((score, str(path), operation, schema))

    if not candidates:
        return None

    score, speech_path, operation, schema = max(candidates, key=lambda item: item[0])
    properties = schema.get("properties", {}) if isinstance(schema.get("properties"), dict) else {}
    text_field = _matching_field(properties, TEXT_FIELD_ALIASES)
    model_field = _matching_field(properties, MODEL_FIELD_ALIASES)
    voice_field = _matching_field(properties, VOICE_FIELD_ALIASES)
    speed_field = _matching_field(properties, SPEED_FIELD_ALIASES)
    format_field = _matching_field(properties, FORMAT_FIELD_ALIASES)
    adapter = (
        OPENAI_ADAPTER
        if speech_path.lower().endswith("/audio/speech") and text_field.lower() == "input"
        else GENERIC_JSON_ADAPTER
    )

    request_fields = {
        "text": text_field or ("input" if adapter == OPENAI_ADAPTER else "text"),
        "model": model_field,
        "voice": voice_field,
        "speed": speed_field,
        "format": format_field,
    }
    excluded_fields = {field for field in (text_field, model_field, voice_field) if field}
    models = _schema_enum(properties.get(model_field), openapi) if model_field else []
    voices = _schema_enum(properties.get(voice_field), openapi) if voice_field else []

    return {
        "adapter": adapter,
        "speech_path": speech_path,
        "models_path": _find_openapi_catalog_path(openapi, "models"),
        "voices_path": _find_openapi_catalog_path(openapi, "voices"),
        "request_fields": request_fields,
        "request_defaults": _schema_defaults(properties, openapi, excluded_fields),
        "models": models,
        "voices": voices,
        "supports_prebuilt_voices": bool(voice_field or voices),
        "score": score,
        "audio_response_documented": _response_returns_audio(operation),
        "text_field_documented": bool(text_field),
    }


def _extract_catalog(payload: Any, kind: str) -> list[str]:
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        singular = "model" if kind == "models" else "voice"
        candidates = []
        for key in (kind, "data", "items", singular):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
            elif isinstance(value, (str, int)):
                candidates.append(value)
    else:
        return []

    values: list[str] = []
    id_keys = (
        ("id", "model_id", "model", "name")
        if kind == "models"
        else ("id", "voice_id", "speaker_id", "voice", "speaker", "name")
    )
    for item in candidates:
        if isinstance(item, dict):
            value = next((item.get(key) for key in id_keys if item.get(key) is not None), "")
        else:
            value = item
        normalized = str(value or "").strip()
        if normalized:
            values.append(normalized)
    return _dedupe(values)


def _suggested_name(base_url: str, openapi: dict[str, Any] | None, adapter: str) -> str:
    if isinstance(openapi, dict):
        info = openapi.get("info", {})
        if isinstance(info, dict):
            title = str(info.get("title") or "").strip()
            if title and title.lower() not in {"fastapi", "api"}:
                return title

    host = urlparse(base_url).netloc or base_url
    prefix = "OpenAI-compatible TTS" if adapter == OPENAI_ADAPTER else "Custom TTS"
    return f"{prefix} ({host})"


def _confidence(score: int) -> str:
    if score >= 120:
        return "high"
    if score >= 70:
        return "medium"
    return "low"


def discover_tts_endpoint(base_url: str, api_key: str = "") -> dict[str, Any]:
    normalized_base_url = normalize_endpoint_base_url(base_url)
    if not normalized_base_url:
        return {
            "success": False,
            "confidence": "none",
            "message": "API base URL is required.",
            "evidence": [],
            "warnings": [],
        }

    evidence: list[str] = []
    warnings: list[str] = []
    openapi: dict[str, Any] | None = None
    openapi_url = ""
    for candidate_url in _openapi_candidate_urls(normalized_base_url):
        result = _safe_get(candidate_url, api_key)
        if result.get("status") == 200 and isinstance(result.get("payload"), dict):
            payload = result["payload"]
            if isinstance(payload.get("paths"), dict):
                openapi = payload
                openapi_url = candidate_url
                evidence.append(f"OpenAPI schema found at {candidate_url}.")
                break

    analysis = _analyze_openapi(openapi) if openapi else None
    all_probe_paths = list(COMMON_MODEL_PATHS + COMMON_VOICE_PATHS)
    if analysis:
        all_probe_paths.extend(
            [
                str(analysis.get("models_path") or ""),
                str(analysis.get("voices_path") or ""),
            ]
        )
    else:
        all_probe_paths.extend(COMMON_SPEECH_PATHS)

    probe_urls = [
        _url_for_path(normalized_base_url, path)
        for path in _dedupe(all_probe_paths)
    ]
    probe_results = _probe_urls(probe_urls, api_key)

    models: list[str] = list(analysis.get("models", [])) if analysis else []
    voices: list[str] = list(analysis.get("voices", [])) if analysis else []
    models_path = str(analysis.get("models_path") or "") if analysis else ""
    voices_path = str(analysis.get("voices_path") or "") if analysis else ""

    for path in _dedupe(list(COMMON_MODEL_PATHS) + ([models_path] if models_path else [])):
        result = probe_results.get(_url_for_path(normalized_base_url, path), {})
        discovered = _extract_catalog(result.get("payload"), "models")
        if discovered:
            models = _dedupe(models + discovered)
            models_path = path
            evidence.append(f"Model catalog found at {path}.")
            break

    for path in _dedupe(list(COMMON_VOICE_PATHS) + ([voices_path] if voices_path else [])):
        result = probe_results.get(_url_for_path(normalized_base_url, path), {})
        discovered = _extract_catalog(result.get("payload"), "voices")
        if discovered:
            voices = _dedupe(voices + discovered)
            voices_path = path
            evidence.append(f"Voice catalog found at {path}.")
            break

    if analysis:
        adapter = str(analysis["adapter"])
        speech_path = str(analysis["speech_path"])
        request_fields = dict(analysis["request_fields"])
        request_defaults = dict(analysis["request_defaults"])
        score = int(analysis["score"]) + (40 if analysis.get("text_field_documented") else 5)
        evidence.append(f"Speech operation inferred from OpenAPI POST {speech_path}.")
        if analysis.get("audio_response_documented"):
            evidence.append("OpenAPI documents an audio response.")
        if not analysis.get("text_field_documented"):
            score = min(score, 60)
            warnings.append(
                "OpenAPI identifies a likely speech route but does not document a supported JSON text field; review the inferred mapping before saving."
            )
    else:
        adapter = ""
        speech_path = ""
        request_fields: dict[str, str] = {}
        request_defaults: dict[str, Any] = {}
        score = 0
        for path in COMMON_SPEECH_PATHS:
            result = probe_results.get(_url_for_path(normalized_base_url, path), {})
            status = int(result.get("status") or 0)
            content_type = str(result.get("content_type") or "")
            route_evidence = status in {400, 405, 415, 422} or (
                status == 200 and ("json" in content_type or "audio" in content_type)
            )
            if not route_evidence:
                continue
            speech_path = path
            adapter = OPENAI_ADAPTER if path.endswith("/audio/speech") else GENERIC_JSON_ADAPTER
            request_fields = {
                "text": "input" if adapter == OPENAI_ADAPTER else "text",
                "model": "model" if adapter == OPENAI_ADAPTER else "",
                "voice": "voice" if adapter == OPENAI_ADAPTER else "",
                "speed": "speed" if adapter == OPENAI_ADAPTER else "",
                "format": "response_format" if adapter == OPENAI_ADAPTER else "",
            }
            score = 85 if adapter == OPENAI_ADAPTER else 55
            evidence.append(f"Likely speech route found at {path} (safe GET returned {status}).")
            if adapter == GENERIC_JSON_ADAPTER:
                warnings.append(
                    "The request field mapping was inferred without OpenAPI documentation; review it before saving."
                )
            break

    if not speech_path:
        return {
            "success": False,
            "confidence": "none",
            "api_base": normalized_base_url,
            "message": "Could not identify a supported speech-generation route.",
            "evidence": evidence,
            "warnings": warnings,
        }

    if models:
        score += 15
    if voices:
        score += 15
    if openapi_url:
        score += 10

    provider = "gemini" if "generativelanguage.googleapis.com" in normalized_base_url.lower() else "openai"
    result = {
        "success": True,
        "confidence": _confidence(score),
        "api_base": normalized_base_url,
        "name": _suggested_name(normalized_base_url, openapi, adapter),
        "provider": provider,
        "adapter": adapter,
        "speech_path": speech_path,
        "models_path": models_path,
        "voices_path": voices_path,
        "request_fields": request_fields,
        "request_defaults": request_defaults,
        "models": models,
        "voices": voices,
        "supports_prebuilt_voices": bool(voices or request_fields.get("voice")),
        "evidence": evidence,
        "warnings": warnings,
    }
    result["message"] = (
        f"Detected {adapter.replace('_', ' ')} adapter with {_confidence(score)} confidence: "
        f"POST {speech_path}."
    )
    return result
