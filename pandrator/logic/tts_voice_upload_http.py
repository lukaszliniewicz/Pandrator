"""Multipart voice upload transport shared by OpenAI-compatible TTS providers."""

import logging
import os
from contextlib import ExitStack

import requests

from .tts_openai_http_policy import (
    _normalize_base_url,
    _openai_audio_voices_urls,
    _openai_auth_headers,
    _openai_files_urls,
    _should_try_next_openai_candidate,
)


def _normalize_upload_wav_paths(wav_file_path: str | list[str]) -> list[str]:
    if isinstance(wav_file_path, str):
        candidates = [wav_file_path]
    elif isinstance(wav_file_path, (list, tuple, set)):
        candidates = [str(candidate or "") for candidate in wav_file_path]
    else:
        raise ValueError("Voice upload expects a WAV path or a list of WAV paths.")

    normalized_paths: list[str] = []
    for candidate in candidates:
        normalized_candidate = str(candidate or "").strip()
        if not normalized_candidate:
            continue
        if not normalized_candidate.lower().endswith(".wav"):
            raise ValueError("Only .wav files are supported for speaker voices.")
        if not os.path.isfile(normalized_candidate):
            raise ValueError(f"WAV file not found: {normalized_candidate}")
        normalized_paths.append(normalized_candidate)

    if not normalized_paths:
        raise ValueError("No WAV files were provided for upload.")

    return normalized_paths


def _extract_uploaded_identifier(payload: object) -> str:
    if isinstance(payload, dict):
        return str(
            payload.get("id") or payload.get("voice_id") or payload.get("name") or ""
        ).strip()

    if isinstance(payload, list):
        for item in payload:
            uploaded_identifier = _extract_uploaded_identifier(item)
            if uploaded_identifier:
                return uploaded_identifier

    return ""


def _upload_speaker_voice_openai_compatible(
    wav_file_path: str | list[str],
    *,
    base_url: str,
    fallback_base_url: str,
    service_name: str,
    api_key: str,
    upload_purpose: str,
    prompt_text: str | None = None,
    mode: str | None = None,
    voice_id: str | None = None,
) -> str:
    wav_file_paths = _normalize_upload_wav_paths(wav_file_path)
    normalized_base_url = _normalize_base_url(base_url, fallback_base_url)
    upload_voice_urls = _openai_audio_voices_urls(normalized_base_url)
    upload_file_urls = _openai_files_urls(normalized_base_url)
    normalized_prompt_text = str(prompt_text or "").strip()
    normalized_mode = str(mode or "").strip().lower()
    first_voice_filename = os.path.basename(wav_file_paths[0])
    fallback_voice_name = os.path.splitext(first_voice_filename)[0]
    resolved_voice_id = (
        str(voice_id or fallback_voice_name).strip() or fallback_voice_name
    )

    try:
        # Preferred path: ecosystem voice endpoint (/v1/audio/voices).
        last_voice_response = None
        for upload_voice_url in upload_voice_urls:
            # Keep both multipart field names for compatibility across API wrappers.
            with ExitStack() as stack:
                files_payload = []
                for sample_path in wav_file_paths:
                    sample_filename = os.path.basename(sample_path)
                    sample_handle = stack.enter_context(open(sample_path, "rb"))
                    files_payload.append(
                        (
                            "files",
                            (
                                sample_filename,
                                sample_handle,
                                "audio/wav",
                            ),
                        )
                    )

                audio_sample_handle = stack.enter_context(open(wav_file_paths[0], "rb"))
                files_payload.append(
                    (
                        "audio_sample",
                        (
                            first_voice_filename,
                            audio_sample_handle,
                            "audio/wav",
                        ),
                    )
                )

                form_data = {
                    "voice_id": resolved_voice_id,
                    "name": resolved_voice_id,
                    "purpose": upload_purpose,
                }
                if normalized_prompt_text and len(wav_file_paths) == 1:
                    form_data["prompt_text"] = normalized_prompt_text
                if normalized_mode:
                    form_data["mode"] = normalized_mode

                response = requests.post(
                    upload_voice_url,
                    headers=_openai_auth_headers(api_key),
                    files=files_payload,
                    data=form_data,
                    timeout=120,
                )

            if _should_try_next_openai_candidate(response.status_code):
                last_voice_response = response
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    f"{service_name} voice upload failed ({response.status_code}): {response.text}"
                )

            try:
                payload = response.json()
            except ValueError:
                payload = {}

            uploaded_voice_id = _extract_uploaded_identifier(payload)
            if not uploaded_voice_id:
                raise RuntimeError(
                    f"{service_name} voice upload succeeded but did not return a voice ID."
                )

            logging.info(
                "Uploaded %s voice '%s' via /audio/voices endpoint (%d sample(s))",
                service_name,
                uploaded_voice_id,
                len(wav_file_paths),
            )
            return uploaded_voice_id

        # Legacy path: OpenAI-compatible files endpoint (/v1/files).
        last_response = None
        for upload_url in upload_file_urls:
            uploaded_file_id = ""
            for sample_path in wav_file_paths:
                sample_filename = os.path.basename(sample_path)
                with open(sample_path, "rb") as wav_file:
                    files = {
                        "file": (
                            sample_filename,
                            wav_file,
                            "audio/wav",
                        )
                    }
                    form_data = {
                        "voice_id": resolved_voice_id,
                        "name": resolved_voice_id,
                        "purpose": upload_purpose,
                    }
                    if normalized_prompt_text and len(wav_file_paths) == 1:
                        form_data["prompt_text"] = normalized_prompt_text
                    if normalized_mode:
                        form_data["mode"] = normalized_mode

                    response = requests.post(
                        upload_url,
                        headers=_openai_auth_headers(api_key),
                        files=files,
                        data=form_data,
                        timeout=120,
                    )

                if _should_try_next_openai_candidate(response.status_code):
                    last_response = response
                    uploaded_file_id = ""
                    break

                if response.status_code >= 400:
                    raise RuntimeError(
                        f"{service_name} voice upload failed ({response.status_code}): {response.text}"
                    )

                try:
                    payload = response.json()
                except ValueError:
                    payload = {}

                uploaded_file_id = _extract_uploaded_identifier(payload)
                if not uploaded_file_id:
                    raise RuntimeError(
                        f"{service_name} file upload succeeded but did not return a file ID."
                    )

            if uploaded_file_id:
                logging.info(
                    "Uploaded %s voice file '%s' via OpenAI-compatible endpoint (%d sample(s))",
                    service_name,
                    uploaded_file_id,
                    len(wav_file_paths),
                )
                return uploaded_file_id

        if last_voice_response is not None and _should_try_next_openai_candidate(
            last_voice_response.status_code
        ):
            logging.debug(
                "%s server at %s does not expose /audio/voices upload; tried /files fallback.",
                service_name,
                normalized_base_url,
            )

        if last_response is not None and _should_try_next_openai_candidate(
            last_response.status_code
        ):
            raise RuntimeError(
                f"{service_name} server at {normalized_base_url} does not support voice upload endpoints (/audio/voices or /files)."
            )

        raise RuntimeError(f"Could not upload speaker voice to {service_name} server.")
    except requests.exceptions.RequestException as e:
        raise RuntimeError(
            f"Failed uploading voice to {service_name} server {normalized_base_url}: {e}"
        ) from e
    except OSError as e:
        raise RuntimeError(f"Could not read WAV file for upload: {e}") from e
