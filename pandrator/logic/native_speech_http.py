"""HTTP transport for prepared native speech requests."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NotRequired, TypeAlias, TypedDict

import requests

JsonValue: TypeAlias = (
    bool | int | float | str | None | Sequence["JsonValue"] | Mapping[str, "JsonValue"]
)


class NativeSpeechPostOptions(TypedDict):
    headers: dict[str, str]
    timeout: float
    data: NotRequired[str]
    json: NotRequired[Mapping[str, JsonValue]]
    params: NotRequired[Mapping[str, str | int]]


class SpeechCandidatePostOptions(TypedDict):
    json: Mapping[str, JsonValue]
    timeout: float
    headers: NotRequired[dict[str, str]]


def post_native_speech(
    url: str,
    *,
    request_label: str,
    request_options: Callable[[], NativeSpeechPostOptions],
) -> requests.Response:
    """Build attempt options inside the transport's exception boundary."""
    try:
        return requests.post(url, **request_options())
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
        response = requests.post(url, **request_options())
        if should_try_next(response.status_code):
            last_response = response
            continue
        return response

    if last_response is not None:
        return last_response

    raise RuntimeError(no_endpoint_message)
