import tempfile
import unittest
from unittest import mock

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from tests.web_test_support import prepare_web_test_data_root


class AudioCppCatalogueApiTests(unittest.TestCase):
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
        response = self.client.post(
            "/api/v1/auth/bootstrap",
            json={"token": token},
        )
        self.assertEqual(200, response.status_code, response.get_json())

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_catalogue_requires_authentication(self):
        with self.client.session_transaction() as session:
            session.clear()
        response = self.client.get("/api/v1/services/audio-cpp/catalogue")
        self.assertEqual(401, response.status_code)

    def test_catalogue_forwards_bounded_filters_without_refreshing_runtime(self):
        payload = {
            "runtime_version": "fixture",
            "source_url": "https://example.test/audio.cpp",
            "total": 1,
            "offset": 3,
            "limit": 17,
            "next_offset": None,
            "items": [{"id": "fixture-model"}],
            "families": [],
        }
        with mock.patch(
            "pandrator.web.api_routes.catalogue_page",
            return_value=payload,
        ) as catalogue_page:
            response = self.client.get(
                "/api/v1/services/audio-cpp/catalogue",
                query_string={
                    "category": "tts",
                    "family": "qwen",
                    "query": "fixture",
                    "language": "en",
                    "capability": "streaming",
                    "commercial_use": "permitted",
                    "recommended_only": "true",
                    "limit": "17",
                    "offset": "3",
                },
            )

        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual(payload, response.get_json())
        catalogue_page.assert_called_once_with(
            category="tts",
            family="qwen",
            query="fixture",
            language="en",
            capability="streaming",
            commercial_use="permitted",
            recommended_only=True,
            limit=17,
            offset=3,
        )

    def test_catalogue_rejects_malformed_or_out_of_range_query_values(self):
        invalid_queries = (
            {"category": "x" * 161},
            {"commercial_use": "permitted_with_attribution"},
            {"commercial_use": "commercial"},
            {"recommended_only": "maybe"},
            {"limit": "0"},
            {"limit": "101"},
            {"limit": "not-an-int"},
            {"offset": "-1"},
            {"offset": "10001"},
            {"offset": "not-an-int"},
        )
        for query_string in invalid_queries:
            with self.subTest(query_string=query_string):
                response = self.client.get(
                    "/api/v1/services/audio-cpp/catalogue",
                    query_string=query_string,
                )
                self.assertEqual(422, response.status_code, response.get_json())
                self.assertEqual("validation_error", response.get_json()["error"]["code"])

    def test_openapi_declares_read_scope_and_query_bounds(self):
        document = self.client.get("/api/v1/openapi.json").get_json()
        operation = document["paths"]["/api/v1/services/audio-cpp/catalogue"]["get"]
        self.assertIn({"nativeOAuth": ["app.read"]}, operation["security"])
        parameters = {item["name"]: item["schema"] for item in operation["parameters"]}
        self.assertEqual(160, parameters["query"]["maxLength"])
        self.assertEqual(
            ["", "permitted", "noncommercial", "conditional", "unknown"],
            parameters["commercial_use"]["enum"],
        )
        self.assertEqual(1, parameters["limit"]["minimum"])
        self.assertEqual(100, parameters["limit"]["maximum"])
        self.assertEqual(0, parameters["offset"]["minimum"])
        self.assertEqual(10_000, parameters["offset"]["maximum"])


if __name__ == "__main__":
    unittest.main()
