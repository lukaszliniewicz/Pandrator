"""Literal HTTP contracts for the public provider voice deletion helper."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from unittest import mock
from urllib.parse import quote

import pytest
import requests

from pandrator.logic import tts_handler

BASE_URL = "http://provider.example/v1"
API_KEY = "fixture-key"
VOICE_ID = "target-voice"
COLLECTION_URLS = (f"{BASE_URL}/audio/voices", f"{BASE_URL}/voices")
HEADERS = {"Authorization": f"Bearer {API_KEY}"}
HttpResult = requests.Response | requests.exceptions.RequestException
HttpCall = tuple[str, str, dict[str, str], int]


def response(
    status: int, payload: object = None, *, invalid_json: bool = False
) -> requests.Response:
    """Use requests' real JSON/status behavior with an eagerly populated body."""
    result = requests.Response()
    result.status_code = status
    result._content = b"not JSON" if invalid_json else json.dumps(payload).encode("utf-8")
    result.encoding = "utf-8"
    return result


class HttpScript:
    """Substitute only requests.get/delete and forbid unplanned HTTP calls."""

    def __init__(self, deletes: tuple[HttpResult, ...], gets: tuple[HttpResult, ...]):
        self.deletes = list(deletes)
        self.gets = list(gets)
        self.calls: list[HttpCall] = []

    def _next(
        self, method: str, url: str, headers: dict[str, str], timeout: int
    ) -> requests.Response:
        self.calls.append((method, url, headers.copy(), timeout))
        pending = self.deletes if method == "DELETE" else self.gets
        assert pending, f"Unexpected {method} call: {url}"
        result = pending.pop(0)
        if isinstance(result, requests.exceptions.RequestException):
            raise result
        return result

    def delete(self, url: str, *, headers: dict[str, str], timeout: int) -> requests.Response:
        return self._next("DELETE", url, headers, timeout)

    def get(self, url: str, *, headers: dict[str, str], timeout: int) -> requests.Response:
        return self._next("GET", url, headers, timeout)

    @contextmanager
    def installed(self) -> Iterator[None]:
        with (
            mock.patch.object(tts_handler.requests, "delete", side_effect=self.delete),
            mock.patch.object(tts_handler.requests, "get", side_effect=self.get),
        ):
            yield

    def assert_calls(self, voice_id: str, *, deletes: int, gets: int) -> None:
        encoded_id = quote(voice_id.strip(), safe="")
        expected: list[HttpCall] = [
            ("DELETE", f"{url}/{encoded_id}", HEADERS, 30) for url in COLLECTION_URLS[:deletes]
        ] + [("GET", url, HEADERS, 8) for url in COLLECTION_URLS[:gets]]
        assert self.calls == expected
        assert not self.deletes and not self.gets, "Planned HTTP responses were not consumed"
        assert all("/files" not in url for _, url, _, _ in self.calls)


@dataclass(frozen=True)
class DeleteCase:
    id: str
    deletes: tuple[HttpResult, ...]
    gets: tuple[HttpResult, ...]
    expected: bool | type[Exception]
    voice_id: str = VOICE_ID
    error_match: str = ""
    cause: requests.exceptions.RequestException | None = None


def literal_cases() -> list[DeleteCase]:
    cases: list[DeleteCase] = []
    for status in (200, 201, 204):
        cases.append(
            DeleteCase(
                f"A-success-{status}",
                (response(status),),
                (),
                True,
                voice_id="  Narrator 主声+1  ",
            )
        )
    for status in (404, 410, 405, 501):
        cases.append(
            DeleteCase(f"B-fallback-{status}", (response(status), response(204)), (), True)
        )
    for first, second in ((404, 404), (410, 410), (405, 501), (404, 501)):
        cases.append(
            DeleteCase(
                f"C-verified-absence-{first}-{second}",
                (response(first), response(second)),
                (response(200, {"data": []}),),
                False,
            )
        )
    present_payloads = (
        {"data": [{"id": VOICE_ID}]},
        {"voices": [VOICE_ID]},
        ["  TARGET-VOICE  "],
        {"data": [{"name": VOICE_ID}]},
    )
    for index, payload in enumerate(present_payloads):
        cases.append(
            DeleteCase(
                f"D-present-{index}",
                (response(404), response(404)),
                (response(200, payload),),
                RuntimeError,
                error_match="still lists",
            )
        )
    unavailable_pairs: tuple[tuple[str, tuple[HttpResult, ...]], ...] = (
        (
            "connection-errors",
            (
                requests.exceptions.ConnectionError("first"),
                requests.exceptions.ConnectionError("second"),
            ),
        ),
        ("missing-unsupported", (response(404), response(501))),
        ("unauthorized-forbidden", (response(401), response(403))),
        ("invalid-json", (response(200, invalid_json=True), response(200, invalid_json=True))),
    )
    for name, gets in unavailable_pairs:
        cases.append(
            DeleteCase(
                f"E-unknown-{name}",
                (response(404), response(404)),
                gets,
                RuntimeError,
                error_match="could not be verified",
            )
        )
    malformed_payloads = (
        None,
        17,
        "unrecognized",
        {},
        {"error": "busy"},
        {"data": {}},
        {"voices": {}},
        {"data": [{}]},
        {"data": [None]},
        {"data": [""]},
        {"data": [{"id": ""}]},
        {"data": [{"id": "other"}, {}]},
        {"error": "busy", "data": []},
    )
    for index, payload in enumerate(malformed_payloads):
        cases.append(
            DeleteCase(
                f"F-unknown-payload-{index}",
                (response(404), response(404)),
                (response(200, payload), response(200, payload)),
                RuntimeError,
                error_match="could not be verified",
            )
        )
    for name, payload, expected in (
        ("absent", {"data": []}, False),
        ("present", {"data": [{"id": VOICE_ID}]}, RuntimeError),
    ):
        cases.append(
            DeleteCase(
                f"G-valid-second-catalogue-{name}",
                (response(404), response(404)),
                (response(200, {}), response(200, payload)),
                expected,
                error_match="still lists" if expected is RuntimeError else "",
            )
        )
    for status in (401, 403, 429, 500):
        cases.append(
            DeleteCase(
                f"H-terminal-{status}",
                (response(status, {"error": "rejected"}),),
                (),
                RuntimeError,
                error_match=str(status),
            )
        )
    for error in (
        requests.exceptions.ConnectionError("delete disconnected"),
        requests.exceptions.Timeout("delete timed out"),
    ):
        cases.append(
            DeleteCase(
                f"I-delete-{type(error).__name__}",
                (error,),
                (),
                RuntimeError,
                cause=error,
            )
        )
    unsafe_ids = (
        "",
        "   ",
        ".",
        "..",
        "../v",
        "v/part",
        "v\\part",
        "v\x00part",
        "v\npart",
        "v" * 256,
    )
    for index, unsafe_id in enumerate(unsafe_ids):
        cases.append(DeleteCase(f"J-unsafe-{index}", (), (), ValueError, voice_id=unsafe_id))
    for index, payload in enumerate(
        ([], {"voices": []}, {"data": [], "voices": []}, {"data": [17]}, {"data": [{"id": 17}]})
    ):
        cases.append(
            DeleteCase(
                f"K-recognized-absence-{index}",
                (response(404), response(404)),
                (response(200, payload),),
                False,
            )
        )
    assert len(cases) == 55
    return cases


@pytest.mark.parametrize("case", literal_cases(), ids=lambda case: case.id)
def test_public_voice_delete_literal_http_contract(case: DeleteCase) -> None:
    script = HttpScript(case.deletes, case.gets)
    result: bool | None = None
    caught: Exception | None = None
    with script.installed():
        try:
            result = tts_handler.delete_speaker_voice(
                case.voice_id, service="Chatterbox", base_url=BASE_URL, api_key=API_KEY
            )
        except Exception as error:
            caught = error
    if isinstance(case.expected, bool):
        assert caught is None, f"Unexpected exception: {caught!r}"
        assert result is case.expected
    else:
        assert isinstance(caught, case.expected), (
            f"Expected {case.expected.__name__}, got result {result!r}; HTTP calls: {script.calls!r}"
        )
        if case.error_match:
            assert case.error_match in str(caught)
        if case.cause is not None:
            assert caught.__cause__ is case.cause
    script.assert_calls(case.voice_id, deletes=len(case.deletes), gets=len(case.gets))


@pytest.mark.parametrize(
    ("payload", "outcome"),
    (
        ({"data": [], "voices": {}}, "unknown"),
        ({"data": [{"id": {}}]}, "unknown"),
        ({"data": [["other"]]}, "unknown"),
        ({"data": [VOICE_ID, {}]}, "present"),
        ({"data": [VOICE_ID], "voices": {}}, "present"),
        ({"error": "busy", "data": [VOICE_ID]}, "present"),
        ([VOICE_ID, None], "present"),
        ({"data": [{"id": "", "name": "other"}]}, "absent"),
    ),
)
def test_partial_catalogue_preserves_presence_and_requires_interpretable_absence(
    payload: object, outcome: str
) -> None:
    deletes = (response(404), response(404))
    gets = (response(200, payload),) * (2 if outcome == "unknown" else 1)
    script = HttpScript(deletes, gets)
    with script.installed():
        if outcome == "absent":
            assert (
                tts_handler.delete_speaker_voice(
                    VOICE_ID, service="Chatterbox", base_url=BASE_URL, api_key=API_KEY
                )
                is False
            )
        else:
            message = "still lists" if outcome == "present" else "could not be verified"
            with pytest.raises(RuntimeError, match=message):
                tts_handler.delete_speaker_voice(
                    VOICE_ID, service="Chatterbox", base_url=BASE_URL, api_key=API_KEY
                )
    script.assert_calls(VOICE_ID, deletes=2, gets=len(gets))
