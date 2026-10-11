"""Stable credential and project identities for native Google stored voices."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import google.auth

from . import tts_handler

_GOOGLE_CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _native_api_base(service_id: str) -> str:
    if service_id == "gemini":
        return tts_handler.GEMINI_AUDIO_BASE_URL
    if service_id == "vertex_ai":
        return "https://aiplatform.googleapis.com"
    raise ValueError("Google voice design supports Gemini and Vertex AI only.")


def _json_project_id(credential: str) -> str:
    try:
        parsed = json.loads(credential)
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(parsed, dict):
        return ""
    return str(parsed.get("project_id") or "").strip()


def google_voice_scope(
    service_id: str,
    service: dict[str, Any],
    credential: str,
) -> dict[str, str]:
    """Return the stable provider identity that fences one design request."""

    expected_base = _native_api_base(service_id)
    if str(service.get("api_base") or "").rstrip("/") != expected_base:
        raise ValueError("Google voice design requires the native Google endpoint.")

    credential_value = str(credential or "").strip()
    if service_id == "gemini":
        if not credential_value:
            raise ValueError("A Gemini API key is required for voice design.")
        project = ""
        identity = hashlib.sha256(credential_value.encode("utf-8")).hexdigest()
    else:
        override_project = str(service.get("vertex_project") or "").strip()
        credential_project = _json_project_id(credential_value)
        adc_project = str(service.get("_google_project") or "").strip()
        detected_project = ""
        adc_credentials_available = bool(adc_project)
        needs_adc = not credential_value or not (
            override_project or credential_project or adc_project
        )
        if needs_adc and not adc_project:
            try:
                credentials, detected = google.auth.default(
                    scopes=[_GOOGLE_CLOUD_SCOPE]
                )
            except Exception:
                if not credential_value:
                    raise ValueError(
                        "Google Vertex AI credentials are not configured."
                    ) from None
                credentials, detected = None, None
            if credentials is None and not credential_value:
                raise ValueError(
                    "Google Vertex AI credentials are not configured."
                )
            adc_credentials_available = credentials is not None
            detected_project = str(detected or "").strip()
        project = (
            override_project
            or credential_project
            or adc_project
            or detected_project
        )
        if not project:
            raise ValueError("A Google Vertex AI project is required for voice design.")
        if not credential_value and not adc_credentials_available:
            raise ValueError("Google Vertex AI credentials are not configured.")
        identity = project

    fingerprint_payload = json.dumps(
        [service_id, expected_base, identity],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "fingerprint": hashlib.sha256(
            fingerprint_payload.encode("utf-8")
        ).hexdigest(),
        "project": project,
    }
