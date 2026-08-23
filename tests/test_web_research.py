import copy
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
    batch_research_source,
    research_source_token_budget,
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
    def test_empty_completion_stops_instead_of_restarting_the_retry_budget(self):
        provider = _FakeResearchProvider()
        calls = []

        def empty_completion(**kwargs):
            calls.append(kwargs)
            return "   "

        result = run_web_research_agent(
            "Text with an uncertain term.",
            provider=provider,
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            config=ResearchAgentConfig(
                stage="correction",
                max_iterations=8,
            ),
            completion_func=empty_completion,
        )

        self.assertEqual(1, len(calls))
        self.assertEqual(1, result.response_count)
        self.assertEqual([], provider.search_calls)
        self.assertEqual([], provider.read_calls)
        self.assertIn("provider retry budget", result.summary)
        self.assertTrue(
            any("provider retry budget" in warning for warning in result.warnings)
        )

    def test_native_tool_calls_preserve_assistant_state_and_use_tool_messages(self):
        provider = _FakeResearchProvider()
        calls = []
        signature = "opaque-thought-signature"

        def tool_response(call_id, name, arguments):
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments),
                },
                "extra_content": {"google": {"thought_signature": signature}},
            }
            assistant = {
                "role": "assistant",
                "content": None,
                "tool_calls": [tool_call],
            }
            return SimpleNamespace(
                content="",
                tool_calls=[tool_call],
                assistant_message=assistant,
                usage={},
                cost=None,
                cost_source="",
            )

        responses = iter(
            [
                tool_response(
                    "call-search",
                    "search_web",
                    {"query": "Nautilus official spelling"},
                ),
                tool_response(
                    "call-finish",
                    "finish",
                    {
                        "summary": "Verified one term.",
                        "evidence": [
                            {
                                "term": "Nautilus",
                                "recommendation": "Nautilus",
                                "claim": "This is the official spelling.",
                                "source_url": "https://example.com/guide",
                                "source_title": "Official terminology",
                                "excerpt": "The source uses this spelling.",
                            }
                        ],
                        "glossary": [],
                    },
                ),
            ]
        )

        def completion(**kwargs):
            calls.append(copy.deepcopy(kwargs))
            return next(responses)

        result = run_web_research_agent(
            "Captain Nemo commanded the Nautilus.",
            provider=provider,
            model_name="vertex_ai/gemini-3-flash",
            llm_settings=SimpleNamespace(),
            config=ResearchAgentConfig(stage="correction"),
            completion_func=completion,
        )

        self.assertEqual(2, len(calls))
        self.assertEqual("auto", calls[0]["tool_choice"])
        self.assertNotIn("max_tokens", calls[0])
        self.assertNotIn("max_tokens", calls[1])
        assistant_turn = calls[1]["messages"][-2]
        tool_turn = calls[1]["messages"][-1]
        self.assertEqual(
            signature,
            assistant_turn["tool_calls"][0]["extra_content"]["google"][
                "thought_signature"
            ],
        )
        self.assertEqual("tool", tool_turn["role"])
        self.assertEqual("call-search", tool_turn["tool_call_id"])
        self.assertEqual(1, len(result.evidence))

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
        self.assertTrue(any("not returned" in warning for warning in result.warnings))

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

    def test_jina_reader_preserves_external_max_tokens_header(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = prepare_web_test_data_root(directory)
            database = Database(paths.database)
            http = _FakeHttpSession()
            provider = JinaResearchProvider(
                api_key="secret-value",
                cache=PersistentResearchCache(database),
                http_session=http,
            )

            provider.read_url("https://example.com/guide", max_tokens=7_500)

            self.assertEqual(1, len(http.calls))
            self.assertEqual(
                "7500",
                http.calls[0][1]["headers"]["X-Max-Tokens"],
            )
            database.dispose()

    def test_context_batches_cover_every_character_within_budget(self):
        source = ("A paragraph with ordinary ASCII words.\n\n" * 300) + (
            "Zażółć gęślą jaźń.\n" * 100
        )
        batches = batch_research_source(
            source,
            context_window_tokens=16_384,
            input_fraction=0.8,
            reserved_prompt_tokens=1_000,
        )

        self.assertEqual(source, "".join(batch.text for batch in batches))
        self.assertEqual(list(range(len(batches))), [batch.index for batch in batches])
        budget = research_source_token_budget(
            16_384,
            input_fraction=0.8,
            reserved_prompt_tokens=1_000,
        )
        self.assertTrue(all(batch.estimated_tokens <= budget for batch in batches))

    def test_research_resumes_after_a_persisted_tool_turn(self):
        provider = _FakeResearchProvider()
        checkpoint = {}

        class CheckpointSaved(RuntimeError):
            pass

        def save_and_stop(state):
            checkpoint.update(state)
            raise CheckpointSaved

        with self.assertRaises(CheckpointSaved):
            run_web_research_agent(
                "Nautilus",
                provider=provider,
                model_name="local/test",
                llm_settings=SimpleNamespace(),
                config=ResearchAgentConfig(stage="correction"),
                completion_func=lambda **_kwargs: json.dumps(
                    {"action": "search_web", "arguments": {"query": "Nautilus"}}
                ),
                on_checkpoint=save_and_stop,
            )

        result = run_web_research_agent(
            "Nautilus",
            provider=provider,
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            config=ResearchAgentConfig(stage="correction"),
            completion_func=lambda **_kwargs: json.dumps(
                {
                    "action": "finish",
                    "summary": "Verified.",
                    "evidence": [
                        {
                            "term": "Nautilus",
                            "recommendation": "Nautilus",
                            "claim": "Official spelling.",
                            "source_url": "https://example.com/guide",
                        }
                    ],
                    "glossary": [],
                }
            ),
            resume_state=checkpoint,
        )

        self.assertEqual(["Nautilus"], provider.search_calls)
        self.assertEqual(1, len(result.evidence))
        self.assertEqual(2, result.response_count)


if __name__ == "__main__":
    unittest.main()
