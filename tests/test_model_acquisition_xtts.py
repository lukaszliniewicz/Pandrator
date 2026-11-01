"""XTTS inference can prepare model weights, so each attempt needs authorization."""

from unittest.mock import Mock

import pytest
import requests

from pandrator.logic import tts_handler
from pandrator.logic.model_acquisition import (
    ModelAcquisitionDenied,
    model_acquisition_scope,
)


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, dict]:
    payload = {"model": "xtts_v2", "input": "Synthetic speech", "voice": "default"}
    monkeypatch.setattr(tts_handler, "_build_xtts_openai_payload", lambda *_: payload)
    monkeypatch.setattr(tts_handler, "_openai_auth_headers", lambda: {"X-Test": "synthetic"})
    monkeypatch.setattr(
        tts_handler,
        "_openai_audio_speech_urls",
        lambda _: [
            "http://synthetic.invalid/v1/audio/speech",
            "http://synthetic.invalid/audio/speech",
        ],
    )
    response = requests.Response()
    response.status_code = 200
    post = Mock(return_value=response)
    monkeypatch.setattr(requests, "post", post)
    return post, payload


def test_unbound_xtts_request_denies_before_transport(transport: tuple[Mock, dict]) -> None:
    post, _payload = transport
    with model_acquisition_scope(None), pytest.raises(ModelAcquisitionDenied):
        tts_handler._request_xtts_audio("Synthetic speech", {}, "http://synthetic.invalid")
    post.assert_not_called()


def test_authorized_xtts_request_keeps_payload_and_response(transport: tuple[Mock, dict]) -> None:
    post, payload = transport
    authorizer = Mock(return_value=None)
    with model_acquisition_scope(authorizer):
        response = tts_handler._request_xtts_audio(
            "Synthetic speech", {}, "http://synthetic.invalid"
        )
    assert response is post.return_value
    assert post.call_args.kwargs["json"] is payload
    assert post.call_args.kwargs["headers"] == {"X-Test": "synthetic"}
    authorizer.assert_called_once_with()


def test_xtts_fallback_attempt_rechecks_revoked_authority(transport: tuple[Mock, dict]) -> None:
    post, _payload = transport
    post.return_value.status_code = 404
    authorizer = Mock(side_effect=[None, PermissionError("Synthetic revoked credential")])
    with model_acquisition_scope(authorizer), pytest.raises(ModelAcquisitionDenied):
        tts_handler._request_xtts_audio("Synthetic speech", {}, "http://synthetic.invalid")
    assert post.call_count == 1
    assert authorizer.call_count == 2


def test_xtts_denial_does_not_get_retried(transport: tuple[Mock, dict]) -> None:
    post, _payload = transport
    authorizer = Mock(side_effect=PermissionError("Synthetic missing Administrator role"))
    with model_acquisition_scope(authorizer), pytest.raises(ModelAcquisitionDenied):
        tts_handler._request_xtts_audio("Synthetic speech", {}, "http://synthetic.invalid")
    post.assert_not_called()
    authorizer.assert_called_once_with()
