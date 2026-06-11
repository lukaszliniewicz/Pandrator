import unittest
from unittest.mock import patch

from pandrator.logic import tts_endpoint_discovery


class FakeResponse:
    def __init__(self, status_code=404, payload=None, content_type="application/json"):
        self.status_code = status_code
        self._payload = payload
        self.headers = {"Content-Type": content_type}
        self.text = "" if payload is None else "{}"

    def json(self):
        return self._payload


def response_map(mapping):
    def fake_get(url, **_kwargs):
        return mapping.get(url, FakeResponse())

    return fake_get


class TTSEndpointDiscoveryTests(unittest.TestCase):
    def test_discovers_generic_styletts_openapi_mapping(self):
        openapi = {
            "info": {"title": "StyleTTS2 API"},
            "paths": {
                "/generate": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["text"],
                                        "properties": {
                                            "text": {"type": "string"},
                                            "speaker": {"type": "string", "enum": ["alice", "bob"]},
                                            "speed": {"type": "number", "default": 1.0},
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {"content": {"audio/wav": {"schema": {"type": "string"}}}}
                        },
                    }
                }
            },
        }
        with patch(
            "pandrator.logic.tts_endpoint_discovery.requests.get",
            side_effect=response_map(
                {"http://localhost:8000/openapi.json": FakeResponse(200, openapi)}
            ),
        ):
            result = tts_endpoint_discovery.discover_tts_endpoint("localhost:8000")

        self.assertTrue(result["success"])
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["adapter"], "generic_json")
        self.assertEqual(result["speech_path"], "/generate")
        self.assertEqual(result["request_fields"]["text"], "text")
        self.assertEqual(result["request_fields"]["voice"], "speaker")
        self.assertEqual(result["request_defaults"], {"speed": 1.0})
        self.assertEqual(result["voices"], ["alice", "bob"])
        self.assertEqual(result["name"], "StyleTTS2 API")

    def test_discovers_openai_compatible_openapi_mapping(self):
        openapi = {
            "paths": {
                "/v1/audio/speech": {
                    "post": {
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "input": {"type": "string"},
                                            "model": {"type": "string", "enum": ["tts-local"]},
                                            "voice": {"type": "string", "enum": ["nova"]},
                                            "response_format": {"type": "string"},
                                        },
                                    }
                                }
                            }
                        },
                        "responses": {
                            "200": {"content": {"audio/mpeg": {"schema": {"type": "string"}}}}
                        },
                    }
                }
            }
        }
        with patch(
            "pandrator.logic.tts_endpoint_discovery.requests.get",
            side_effect=response_map(
                {"http://localhost:9000/openapi.json": FakeResponse(200, openapi)}
            ),
        ):
            result = tts_endpoint_discovery.discover_tts_endpoint("http://localhost:9000")

        self.assertTrue(result["success"])
        self.assertEqual(result["adapter"], "openai_compatible")
        self.assertEqual(result["speech_path"], "/v1/audio/speech")
        self.assertEqual(result["models"], ["tts-local"])
        self.assertEqual(result["voices"], ["nova"])

    def test_infers_likely_generic_route_without_openapi(self):
        with patch(
            "pandrator.logic.tts_endpoint_discovery.requests.get",
            side_effect=response_map(
                {"http://localhost:8000/generate": FakeResponse(405, None, "text/plain")}
            ),
        ):
            result = tts_endpoint_discovery.discover_tts_endpoint("http://localhost:8000")

        self.assertTrue(result["success"])
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["adapter"], "generic_json")
        self.assertEqual(result["speech_path"], "/generate")
        self.assertTrue(result["warnings"])

    def test_fails_when_no_supported_route_is_found(self):
        with patch(
            "pandrator.logic.tts_endpoint_discovery.requests.get",
            side_effect=response_map({}),
        ):
            result = tts_endpoint_discovery.discover_tts_endpoint("http://localhost:8000")

        self.assertFalse(result["success"])
        self.assertEqual(result["confidence"], "none")

    def test_ignores_blanket_auth_and_html_fallback_responses(self):
        responses = {
            "http://localhost:8000/v1/audio/speech": FakeResponse(401, None, "application/json"),
            "http://localhost:8000/audio/speech": FakeResponse(200, None, "text/html"),
        }
        with patch(
            "pandrator.logic.tts_endpoint_discovery.requests.get",
            side_effect=response_map(responses),
        ):
            result = tts_endpoint_discovery.discover_tts_endpoint("http://localhost:8000")

        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
