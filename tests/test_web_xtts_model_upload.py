import io
import tempfile
import unittest
from unittest import mock

import requests

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.tts_providers import XttsAdapter
from tests.web_test_support import prepare_web_test_data_root

XTTS_MODEL_ENDPOINT = "/api/v1/services/tts/xtts/models"


class XttsModelUploadApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        self.catalogue = self.app.extensions["pandrator"]["tts_catalogue"]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    @staticmethod
    def _bundle():
        return {
            "model_id": "narrator-v1",
            "files": [
                (io.BytesIO(b'{"model": "xtts"}'), "config.json"),
                (io.BytesIO(b"model weights"), "model.pth"),
                (io.BytesIO(b"speakers"), "speakers_xtts.pth"),
                (io.BytesIO(b'{"vocab": []}'), "vocab.json"),
            ],
        }

    @staticmethod
    def _catalogue(endpoint="http://127.0.0.1:8020"):
        return (
            {
                "services": [
                    {
                        "id": "xtts",
                        "api_base": endpoint,
                        "connection_mode": "external",
                    }
                ]
            },
            0,
        )

    def test_requires_authentication(self):
        response = self.app.test_client().post(
            XTTS_MODEL_ENDPOINT,
            data=self._bundle(),
            content_type="multipart/form-data",
        )

        self.assertEqual(401, response.status_code)

    def test_rejects_incomplete_or_nested_bundle_before_proxying(self):
        with mock.patch("pandrator.web.api_routes.requests.post") as post:
            response = self.client.post(
                XTTS_MODEL_ENDPOINT,
                data={
                    "model_id": "narrator-v1",
                    "files": [
                        (io.BytesIO(b"weights"), "checkpoint/model.pth"),
                        (io.BytesIO(b"config"), "config.json"),
                    ],
                },
                content_type="multipart/form-data",
                headers=self.headers,
            )

        self.assertEqual(422, response.status_code)
        self.assertIn("exactly config.json", response.get_json()["error"]["message"])
        post.assert_not_called()

    def test_rejects_invalid_model_id_before_proxying(self):
        with mock.patch("pandrator.web.api_routes.requests.post") as post:
            bundle = self._bundle()
            bundle["model_id"] = "../../training-output"
            response = self.client.post(
                XTTS_MODEL_ENDPOINT,
                data=bundle,
                content_type="multipart/form-data",
                headers=self.headers,
            )

        self.assertEqual(422, response.status_code)
        self.assertIn("model_id", response.get_json()["error"]["message"])
        post.assert_not_called()

    def test_streams_exact_bundle_to_effective_managed_endpoint(self):
        managed_catalogue = self._catalogue("http://stale.invalid:8020")[0]
        managed_catalogue["services"][0].update(
            {
                "connection_mode": "managed_local",
                "manager_service": {"endpoint": "http://127.0.0.1:9234"},
            }
        )
        wrapper_response = mock.Mock(status_code=201)
        wrapper_response.json.return_value = {
            "id": "narrator-v1",
            "object": "model",
            "owned_by": "user",
            "bytes": 1234,
        }
        with (
            mock.patch.object(
                self.catalogue,
                "snapshot",
                return_value=(managed_catalogue, 0),
            ),
            mock.patch(
                "pandrator.web.api_routes.requests.post",
                return_value=wrapper_response,
            ) as post,
        ):
            response = self.client.post(
                XTTS_MODEL_ENDPOINT,
                data=self._bundle(),
                content_type="multipart/form-data",
                headers=self.headers,
            )

        self.assertEqual(201, response.status_code, response.get_json())
        self.assertEqual("narrator-v1", response.get_json()["id"])
        self.assertEqual("http://127.0.0.1:9234/v1/models", post.call_args.args[0])
        self.assertNotIsInstance(post.call_args.kwargs["data"], bytes)
        self.assertEqual((10, 3600), post.call_args.kwargs["timeout"])
        self.assertIn(
            "multipart/form-data; boundary=",
            post.call_args.kwargs["headers"]["Content-Type"],
        )
        prepared = requests.Request(
            "POST",
            post.call_args.args[0],
            data=post.call_args.kwargs["data"],
            headers=post.call_args.kwargs["headers"],
        ).prepare()
        self.assertEqual(
            str(len(post.call_args.kwargs["data"])),
            prepared.headers["Content-Length"],
        )
        self.assertNotIn("Transfer-Encoding", prepared.headers)

    def test_preserves_wrapper_rejection_and_explains_missing_upload_support(self):
        wrapper_response = mock.Mock(status_code=404)
        wrapper_response.json.return_value = {"detail": "Not Found"}
        with (
            mock.patch.object(
                self.catalogue,
                "snapshot",
                return_value=self._catalogue(),
            ),
            mock.patch(
                "pandrator.web.api_routes.requests.post",
                return_value=wrapper_response,
            ),
        ):
            response = self.client.post(
                XTTS_MODEL_ENDPOINT,
                data=self._bundle(),
                content_type="multipart/form-data",
                headers=self.headers,
            )

        self.assertEqual(404, response.status_code)
        self.assertEqual(
            "xtts_model_upload_unsupported",
            response.get_json()["error"]["code"],
        )
        self.assertIn(
            "Update the XTTS component", response.get_json()["error"]["message"]
        )

    def test_preserves_safe_wrapper_validation_message_and_status(self):
        wrapper_response = mock.Mock(status_code=409)
        wrapper_response.json.return_value = {
            "error": {
                "message": "A model with this ID is already installed.",
                "type": "invalid_request_error",
                "param": "model_id",
                "code": "model_already_exists",
            }
        }
        with (
            mock.patch.object(
                self.catalogue,
                "snapshot",
                return_value=self._catalogue(),
            ),
            mock.patch(
                "pandrator.web.api_routes.requests.post",
                return_value=wrapper_response,
            ),
        ):
            response = self.client.post(
                XTTS_MODEL_ENDPOINT,
                data=self._bundle(),
                content_type="multipart/form-data",
                headers=self.headers,
            )

        self.assertEqual(409, response.status_code)
        self.assertEqual(
            "xtts_model_upload_rejected", response.get_json()["error"]["code"]
        )
        self.assertEqual(
            "A model with this ID is already installed.",
            response.get_json()["error"]["message"],
        )

    def test_maps_wrapper_connection_failure(self):
        with (
            mock.patch.object(
                self.catalogue,
                "snapshot",
                return_value=self._catalogue(),
            ),
            mock.patch(
                "pandrator.web.api_routes.requests.post",
                side_effect=requests.ConnectionError,
            ),
        ):
            response = self.client.post(
                XTTS_MODEL_ENDPOINT,
                data=self._bundle(),
                content_type="multipart/form-data",
                headers=self.headers,
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            "xtts_service_unavailable", response.get_json()["error"]["code"]
        )


class XttsCatalogueTests(unittest.TestCase):
    def test_catalogue_merges_default_custom_models_and_existing_voices(self):
        adapter = XttsAdapter("xtts")
        service = {
            "api_base": "http://127.0.0.1:8020",
            "models": ["saved-model"],
            "default_model": "saved-model",
            "voices": ["saved-voice"],
            "voice_catalogues": {"saved-model": ["catalogue-voice"]},
        }
        with (
            mock.patch(
                "pandrator.logic.tts_handler.get_xtts_models",
                return_value=[
                    "tts_models/multilingual/multi-dataset/xtts_v2",
                    "custom-model",
                ],
            ),
            mock.patch(
                "pandrator.logic.tts_handler.get_xtts_speakers",
                return_value=["live-voice"],
            ),
        ):
            catalogue = adapter.enrich_catalog(service)

        self.assertEqual(
            [
                "saved-model",
                "tts_models/multilingual/multi-dataset/xtts_v2",
                "custom-model",
            ],
            catalogue["models"],
        )
        self.assertEqual(["saved-voice", "live-voice"], catalogue["voices"])
        self.assertEqual(
            ["catalogue-voice", "saved-voice", "live-voice"],
            catalogue["voice_catalogues"]["saved-model"],
        )
        capabilities = adapter.capabilities(service)
        self.assertTrue(capabilities.dynamic_catalog)
        self.assertTrue(capabilities.model_upload)


if __name__ == "__main__":
    unittest.main()
