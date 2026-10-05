"""One-shot HTTP transport for prepared native speech requests."""

from collections.abc import Callable, Mapping, Sequence
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
