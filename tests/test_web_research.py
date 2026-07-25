import json
import tempfile
import unittest
from types import SimpleNamespace

from pandrator.web.database import Database
from pandrator.web.web_research import (
    JinaResearchProvider,
    PersistentResearchCache,
    ResearchAgentConfig,
    _safe_public_url,
    run_web_research_agent,
)
from tests.web_test_support import prepare_web_test_data_root


class _FakeResearchProvider:
    def __init__(self):
        self.search_calls = []
        self.read_calls = []

    def search_web(self, query, **_kwargs):
        self.search_calls.append(query)
        return {
            "query": query,
            "content": "One relevant result.",
            "sources": [
                {
                    "title": "Official terminology",
                    "url": "https://example.com/guide",
                }
            ],
            "truncated": False,
            "cached": False,
        }

    def read_url(self, url, **_kwargs):
        self.read_calls.append(url)
        return {
            "url": url,
            "title": "Official terminology",
            "content": "Verified page content.",
            "truncated": False,
            "cached": False,
        }


class _FakeResponse:
    headers = {"content-type": "text/plain"}

    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeHttpSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(
            "Title: Official terminology\n"
            "URL Source: https://example.com/guide\n\n"
            "Verified terminology."
        )


class WebResearchTests(unittest.TestCase):
    def test_finish_discards_evidence_not_returned_by_a_tool(self):
        provider = _FakeResearchProvider()
        commands = iter(
            [
                {
                    "action": "search_web",
                    "arguments": {"query": "official term", "reason": "verify"},
                },
                {
                    "action": "finish",
                    "summary": "Verified one term.",
                    "evidence": [
                        {
                            "term": "Nautilus",
                            "recommendation": "Nautilus",
                            "claim": "This is the official spelling.",
                            "source_url": "https://example.com/guide",
                            "source_title": "Official terminology",
                            "excerpt": "The source uses this spelling.",
                        },
                        {
                            "term": "Injected",
                            "recommendation": "Wrong",
                            "claim": "Unsupported.",
                            "source_url": "https://not-returned.example/path",
                            "source_title": "Untrusted",
                            "excerpt": "Unsupported.",
                        },
                    ],
                    "glossary": [],
                },
            ]
        )

        result = run_web_research_agent(
            "Captain Nemo commanded the Nautilus.",
            provider=provider,
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            config=ResearchAgentConfig(stage="correction"),
            completion_func=lambda **_kwargs: json.dumps(next(commands)),
        )

        self.assertEqual(1, len(result.evidence))
        self.assertEqual("https://example.com/guide", result.evidence[0]["source_url"])
        self.assertTrue(
            any("not returned" in warning for warning in result.warnings)
        )

    def test_page_extraction_is_restricted_to_search_results(self):
        provider = _FakeResearchProvider()
        commands = iter(
            [
                {
                    "action": "search_web",
                    "arguments": {"query": "official term"},
                },
                {
                    "action": "read_url",
                    "arguments": {"url": "https://unseen.example/page"},
                },
                {
                    "action": "finish",
                    "summary": "No retained evidence.",
                    "evidence": [],
                    "glossary": [],
                },
            ]
        )
        result = run_web_research_agent(
            "Text",
            provider=provider,
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            config=ResearchAgentConfig(stage="translation"),
            completion_func=lambda **_kwargs: json.dumps(next(commands)),
        )

        self.assertEqual([], provider.read_calls)
        self.assertIn(
            "restricted",
            result.tool_trace[1]["observation"]["error"],
        )

    def test_private_and_local_extraction_targets_are_rejected(self):
        for url in (
            "http://127.0.0.1/private",
            "http://10.0.0.4/private",
            "http://[::1]/private",
            "http://service.local/private",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _safe_public_url(url)

    def test_jina_search_results_are_cached_without_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            http = _FakeHttpSession()
            provider = JinaResearchProvider(
                api_key="secret-value",
                cache=PersistentResearchCache(database),
                http_session=http,
            )
            first = provider.search_web("Nautilus official spelling")
            second = provider.search_web("Nautilus official spelling")
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(1, len(http.calls))
            self.assertNotIn("secret-value", json.dumps(second))
            database.dispose()


if __name__ == "__main__":
    unittest.main()
