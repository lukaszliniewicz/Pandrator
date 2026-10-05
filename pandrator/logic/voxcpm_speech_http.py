"""VoxCPM speech transport and prompt-pairing compatibility retry."""

from collections.abc import Callable, Iterable, Mapping

import requests

from .native_speech_http import JsonValue, SpeechCandidatePostOptions


def _is_voxcpm_prompt_pairing_error(response: requests.Response) -> bool:
    if response.status_code != 422:
        return False

    error_message = ""
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        error_payload = payload.get("error")
        if isinstance(error_payload, dict):
            error_message = str(error_payload.get("message") or "").strip()

    if not error_message:
        error_message = str(response.text or "").strip()

    normalized = error_message.lower()
    return "prompt_wav_path and prompt_text must both be provided or both be none" in normalized


def post_voxcpm_speech(
    urls: Iterable[str],
    payload: Mapping[str, JsonValue],
    *,
    request_options: Callable[[Mapping[str, JsonValue]], SpeechCandidatePostOptions],
    prompt_pairing_error: Callable[[requests.Response], bool],
    should_try_next: Callable[[int], bool],
    warn: Callable[[str, JsonValue], None],
    no_endpoint_message: str,
) -> requests.Response:
    """Keep the hifi retry local to each candidate and retain the original payload."""
    last_response = None
    for url in urls:
        response = requests.post(url, **request_options(payload))

        if (
            prompt_pairing_error(response)
            and str(payload.get("mode") or "").strip().lower() != "hifi"
        ):
            hifi_payload = dict(payload)
            hifi_payload["mode"] = "hifi"
            warn(
                "Retrying VoxCPM request in hifi mode after prompt pairing error for voice '%s'.",
                hifi_payload.get("voice", ""),
            )
            response = requests.post(url, **request_options(hifi_payload))

        if should_try_next(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(no_endpoint_message)
