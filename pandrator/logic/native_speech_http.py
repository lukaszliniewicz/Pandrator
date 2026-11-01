"""HTTP transport for prepared native speech requests."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NotRequired, TypeAlias, TypedDict

import requests

from .model_acquisition import ModelAcquisitionDenied
from .tts_io_policy import check_tts_io, current_tts_io_policy

JsonValue: TypeAlias = (
    bool | int | float | str | None | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
)


class NativeSpeechPostOptions(TypedDict):
    headers: dict[str, str]
    timeout: float
    allow_redirects: NotRequired[bool]
    data: NotRequired[str]
    json: NotRequired[Mapping[str, JsonValue]]
    params: NotRequired[Mapping[str, str | int]]


class SpeechCandidatePostOptions(TypedDict):
    json: Mapping[str, JsonValue]
    timeout: float
    allow_redirects: NotRequired[bool]
    headers: NotRequired[dict[str, str]]


def require_prepared_model_response(response: requests.Response) -> None:
    """A known missing model needs preparation rather than transient retries."""
    if response.status_code != 409:
        return
    try:
        payload = response.json()
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    error = payload.get("error", payload.get("detail"))
    if isinstance(error, dict) and error.get("code") == "model_acquisition_required":
        raise ModelAcquisitionDenied()


def post_native_speech(
    url: str,
    *,
    request_label: str,
    request_options: Callable[[], NativeSpeechPostOptions],
) -> requests.Response:
    """Build attempt options inside the transport's exception boundary."""
    try:
        post = requests.post
        options: NativeSpeechPostOptions = request_options()
        if current_tts_io_policy() is not None:
            options = {**options, "allow_redirects": False}
        check_tts_io()
        return post(url, **options)
    except requests.exceptions.Timeout as error:
        raise RuntimeError(f"{request_label} request timed out.") from error
    except requests.exceptions.RequestException as error:
        raise RuntimeError(f"{request_label} request failed: {error}") from error


def post_speech_candidates(
    urls: Iterable[str],
    *,
    request_options: Callable[[], SpeechCandidatePostOptions],
    should_try_next: Callable[[int], bool],
    no_endpoint_message: str,
) -> requests.Response:
    """Try compatibility candidates, preserving responses and transport exceptions."""
    last_response = None
    for url in urls:
        post = requests.post
        options: SpeechCandidatePostOptions = request_options()
        if current_tts_io_policy() is not None:
            options = {**options, "allow_redirects": False}
        check_tts_io()
        response = post(url, **options)
        if should_try_next(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(no_endpoint_message)


def post_prepared_speech(
    url: Callable[[], str],
    *,
    request_options: Callable[[], SpeechCandidatePostOptions],
    request_session: requests.Session | None = None,
) -> requests.Response:
    """POST once, preserving transport errors and the caller's session ownership."""
    transport = request_session or requests
    post = transport.post
    destination = url()
    options: SpeechCandidatePostOptions = request_options()
    if current_tts_io_policy() is not None:
        options = {**options, "allow_redirects": False}
    check_tts_io()
    return post(destination, **options)
