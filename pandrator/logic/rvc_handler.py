"""HTTP client and local model-file management for the RVC service."""

from __future__ import annotations

import io
import logging
import os
import shutil

import requests
from pydub import AudioSegment


RVC_API_URL = os.environ.get("PANDRATOR_RVC_API_URL", "http://127.0.0.1:8050").rstrip("/")
RVC_HEALTH_TIMEOUT_SECONDS = 1.0
RVC_REQUEST_TIMEOUT_SECONDS = 600.0


def _response_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", {})
        return str(error.get("message") or error.get("code") or response.text)
    except Exception:
        return response.text or f"HTTP {response.status_code}"


def is_rvc_available() -> bool:
    """Return whether the selected RVC service is online and ready."""
    try:
        response = requests.get(f"{RVC_API_URL}/health", timeout=RVC_HEALTH_TIMEOUT_SECONDS)
        response.raise_for_status()
        return bool(response.json().get("ready"))
    except (requests.RequestException, ValueError):
        return False


def get_rvc_models(rvc_models_dir: str) -> list[str]:
    """Return model names reported by the RVC service."""
    del rvc_models_dir
    try:
        response = requests.get(
            f"{RVC_API_URL}/v1/models",
            timeout=RVC_HEALTH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return [str(model) for model in response.json().get("models", [])]
    except (requests.RequestException, ValueError) as exc:
        logging.warning("Could not list RVC models from %s: %s", RVC_API_URL, exc)
        return []


def process_with_rvc(audio_segment: AudioSegment, settings: dict) -> AudioSegment:
    """Convert an audio segment using the selected model and parameters."""
    model_name = str(settings.get("rvc_model") or "").strip()
    if not model_name:
        logging.warning("No RVC model selected. Skipping RVC processing.")
        return audio_segment

    audio_buffer = io.BytesIO()
    audio_segment.export(audio_buffer, format="wav")
    audio_buffer.seek(0)

    data = {
        "model": model_name,
        "pitch": int(settings.get("pitch", 0)),
        "f0_method": str(settings.get("f0_method", "rmvpe")),
        "index_rate": float(settings.get("index_rate", 0.3)),
        "filter_radius": int(settings.get("filter_radius", 3)),
        "volume_envelope": float(settings.get("volume_envelope", 1.0)),
        "protect": float(settings.get("protect", 0.3)),
        "resample_sr": 40000,
    }

    try:
        response = requests.post(
            f"{RVC_API_URL}/v1/convert",
            files={"audio": ("input.wav", audio_buffer, "audio/wav")},
            data=data,
            timeout=RVC_REQUEST_TIMEOUT_SECONDS,
        )
        if not response.ok:
            raise RuntimeError(_response_error_message(response))
        return AudioSegment.from_file(io.BytesIO(response.content), format="wav")
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        logging.error("RVC processing failed: %s", exc)
        return audio_segment


def upload_rvc_model(pth_file: str, index_file: str, rvc_models_dir: str) -> str:
    """Copy a model into the shared model directory and refresh the service."""
    model_name = os.path.splitext(os.path.basename(pth_file))[0]
    model_dir = os.path.join(rvc_models_dir, model_name)
    os.makedirs(model_dir, exist_ok=True)

    shutil.copy(pth_file, os.path.join(model_dir, f"{model_name}.pth"))
    index_ext = os.path.splitext(index_file)[1]
    shutil.copy(index_file, os.path.join(model_dir, f"{model_name}{index_ext}"))

    try:
        response = requests.post(
            f"{RVC_API_URL}/v1/models/refresh",
            timeout=RVC_HEALTH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logging.warning("RVC model was copied, but the service refresh failed: %s", exc)

    return model_name
