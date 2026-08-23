import json
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pandrator.logic import llm_handler


class LlmHandlerTests(unittest.TestCase):
    def test_litellm_lazy_import_is_atomic_across_worker_threads(self):
        sentinel_module = object()
        sentinel_completion = object()
        sentinel_discovery = object()
        import_calls = 0
        import_started = threading.Event()

        def slow_import():
            nonlocal import_calls
            import_calls += 1
            import_started.set()
            time.sleep(0.05)
            return sentinel_module, sentinel_completion, sentinel_discovery

        original_state = (
            llm_handler._litellm_module,
            llm_handler._litellm_completion,
            llm_handler._litellm_get_valid_models,
            llm_handler._litellm_import_attempted,
            llm_handler._litellm_import_error,
        )
        try:
            llm_handler._litellm_module = None
            llm_handler._litellm_completion = None
            llm_handler._litellm_get_valid_models = None
            llm_handler._litellm_import_attempted = False
            llm_handler._litellm_import_error = None
            with patch(
                "pandrator.logic.llm_handler._import_litellm_clients",
                side_effect=slow_import,
            ):
                with ThreadPoolExecutor(max_workers=12) as executor:
                    first = executor.submit(llm_handler._get_litellm_clients)
                    self.assertTrue(import_started.wait(timeout=1))
                    remaining = [
                        executor.submit(llm_handler._get_litellm_clients)
                        for _index in range(11)
                    ]
                    results = [first.result(), *(future.result() for future in remaining)]
        finally:
            (
                llm_handler._litellm_module,
                llm_handler._litellm_completion,
                llm_handler._litellm_get_valid_models,
                llm_handler._litellm_import_attempted,
                llm_handler._litellm_import_error,
            ) = original_state

        self.assertEqual(1, import_calls)
        self.assertTrue(
            all(result == (sentinel_completion, sentinel_discovery) for result in results)
        )

    def test_custom_endpoint_model_discovery_preserves_full_base_path(self):
        response = Mock(
            status_code=200,
            headers={"content-type": "application/json"},
        )
        response.json.return_value = {
            "data": [
                {"id": "mimo-v2.5"},
                {"id": "google/gemini-3-flash"},
            ]
        }
        http_client = Mock()
        http_client.get.return_value = response
        static_catalogue = Mock(return_value=["gpt-5.4"])

        with patch.object(
            llm_handler,
            "_litellm_module",
            SimpleNamespace(module_level_client=http_client),
        ), patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(object(), static_catalogue),
        ):
            result = llm_handler.discover_provider_models(
                {
                    "provider": "openai",
                    "api_base": "https://opencode.ai/zen/go/v1/",
                    "api_key": "secret",
                    "models": ["manual"],
                }
            )

        self.assertEqual("endpoint", result.source)
        self.assertEqual(
            ("mimo-v2.5", "google/gemini-3-flash"),
            result.models,
        )
        self.assertEqual(
            "https://opencode.ai/zen/go/v1/models",
            http_client.get.call_args.kwargs["url"],
        )
        self.assertEqual(
            "Bearer secret",
            http_client.get.call_args.kwargs["headers"]["Authorization"],
        )
        static_catalogue.assert_not_called()

    def test_custom_endpoint_failure_preserves_models_without_static_fallback(self):
        response = Mock(
            status_code=404,
            headers={"content-type": "text/html; charset=utf-8"},
        )
        http_client = Mock()
        http_client.get.return_value = response
        static_catalogue = Mock(return_value=["gpt-5.4"])

        with patch.object(
            llm_handler,
            "_litellm_module",
            SimpleNamespace(module_level_client=http_client),
        ), patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(object(), static_catalogue),
        ):
            result = llm_handler.discover_provider_models(
                {
                    "provider": "openai",
                    "api_base": "https://example.test/custom/v1",
                    "models": ["manual"],
                }
            )

        self.assertEqual("preserved", result.source)
        self.assertEqual(("manual",), result.models)
        self.assertIn("HTTP 404", result.warning)
        static_catalogue.assert_not_called()

    def test_ollama_discovery_uses_native_path_without_static_fallback(self):
        response = Mock(
            status_code=200,
            headers={"content-type": "application/json"},
        )
        response.json.return_value = {
            "models": [{"name": "qwen3:8b"}, {"model": "gemma3:12b"}]
        }
        http_client = Mock()
        http_client.get.return_value = response

        with patch.object(
            llm_handler,
            "_litellm_module",
            SimpleNamespace(module_level_client=http_client),
        ), patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(object(), Mock()),
        ):
            result = llm_handler.discover_provider_models(
                {
                    "provider": "ollama",
                    "api_base": "http://127.0.0.1:11434",
                }
            )

        self.assertEqual(("qwen3:8b", "gemma3:12b"), result.models)
        self.assertEqual(
            "http://127.0.0.1:11434/api/tags",
            http_client.get.call_args.kwargs["url"],
        )
        self.assertNotIn(
            "Authorization",
            http_client.get.call_args.kwargs["headers"],
        )

    def test_chat_completion_retries_rate_limits_and_honors_retry_after(self):
        class RateLimited(RuntimeError):
            status_code = 429

            def __init__(self):
                super().__init__("rate limited")
                self.response = type("Response", (), {"status_code": 429, "headers": {"Retry-After": "2"}})()

        calls = 0

        def fake_completion(**_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimited()
            return {"choices": [{"message": {"content": "recovered"}}]}

        with patch("pandrator.logic.llm_handler._get_litellm_clients", return_value=(fake_completion, None)), patch(
            "pandrator.logic.llm_handler.wait_for_retry", return_value=True
        ) as wait:
            result = llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "retry"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={"llm_max_attempts": 3},
            )

        self.assertEqual("recovered", result.content)
        self.assertEqual(2, calls)
        self.assertGreaterEqual(wait.call_args.args[0], 2.0)

    def test_chat_completion_does_not_retry_authentication_failures(self):
        class Unauthorized(RuntimeError):
            status_code = 401

        completion = Mock(side_effect=Unauthorized("invalid key"))
        with patch("pandrator.logic.llm_handler._get_litellm_clients", return_value=(completion, None)), patch(
            "pandrator.logic.llm_handler.wait_for_retry"
        ) as wait:
            result = llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "do not retry"}],
                model_name="openai/gpt-5.4-mini",
            )

        self.assertEqual("", result.content)
        self.assertEqual(1, completion.call_count)
        wait.assert_not_called()

    def test_legacy_models_migrate_and_refresh_merge_preserves_settings(self):
        migrated = llm_handler.normalize_model_records(["manual-model"], "openai")
        self.assertEqual(migrated, [llm_handler.default_model_record("manual-model")])
        migrated[0]["default_temperature"] = 0.25
        merged = llm_handler._merge_model_records(
            migrated, ["discovered-model"], "openai"
        )
        self.assertEqual([record["id"] for record in merged], ["manual-model", "discovered-model"])
        self.assertEqual(merged[0]["default_temperature"], 0.25)

    def test_chat_completion_with_metadata_strips_output_limit_options(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {
                "id": "response-1",
                "model": kwargs["model"],
                "choices": [
                    {
                        "message": {
                            "content": "Corrected text",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 5,
                },
            }

        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            result = llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Fix this"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={
                    "request_timeout_seconds": 30,
                    "provider_configs": [
                        {
                            **next(
                                provider
                                for provider in llm_handler.get_provider_configs(None)
                                if provider["id"] == "openai"
                            ),
                            "models": [
                                {
                                    **llm_handler.default_model_record("gpt-5.4-mini"),
                                    "default_temperature": 0.1,
                                    "default_reasoning_effort": "medium",
                                }
                            ],
                            "request_options": {
                                "organization": "pandrator-test",
                                "temperature": 1.9,
                                "MAX-TOKENS": 1234,
                                "max_completion_tokens": 2345,
                                "MaxOutputTokens": 3456,
                            },
                        }
                    ],
                },
            )

        self.assertEqual(result.content, "Corrected text")
        self.assertEqual(captured_payload["temperature"], 0.1)
        self.assertEqual(captured_payload["timeout"], 30)
        self.assertEqual(captured_payload["reasoning_effort"], "medium")
        self.assertEqual(captured_payload["organization"], "pandrator-test")
        for key in ("MAX-TOKENS", "max_completion_tokens", "MaxOutputTokens"):
            self.assertNotIn(key, captured_payload)

    def test_legacy_provider_options_strip_output_limit_aliases(self):
        settings = {
            "custom_openai_endpoints_json": json.dumps(
                [
                    {
                        "name": "legacy",
                        "base_url": "http://127.0.0.1:8000/v1",
                        "default_model": "demo",
                        "request_options": {
                            "max_tokens": 100,
                            "MAX_COMPLETION-TOKENS": 200,
                            "normal_option": "preserved",
                        },
                    }
                ]
            )
        }

        provider = next(
            item for item in llm_handler.get_provider_configs(settings) if item["id"] == "legacy"
        )
        self.assertEqual({"normal_option": "preserved"}, provider["request_options"])

    def test_completion_boundary_strips_aliases_from_resolved_overrides(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

        details = {
            "model": "openai/demo",
            "request_overrides": {
                "mAx_ToKeNs": 1,
                "max-completion-tokens": 2,
                "MAX_OUTPUT_TOKENS": 3,
                "normal_option": "preserved",
            },
            "model_record": None,
        }
        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ), patch(
            "pandrator.logic.llm_handler._resolve_model_request_details",
            return_value=details,
        ):
            result = llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "test"}],
                model_name="openai/demo",
            )

        self.assertEqual("ok", result.content)
        self.assertEqual("preserved", captured_payload["normal_option"])
        self.assertNotIn("mAx_ToKeNs", captured_payload)
        self.assertNotIn("max-completion-tokens", captured_payload)
        self.assertNotIn("MAX_OUTPUT_TOKENS", captured_payload)

    def test_tool_call_is_a_success_and_preserves_gemini_thought_signature(self):
        captured_payload = {}
        thought_signature = "opaque-gemini-state"

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {
                "id": "tool-response-1",
                "model": kwargs["model"],
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-search-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_web",
                                        "arguments": '{"query":"Nautilus"}',
                                    },
                                    "extra_content": {
                                        "google": {
                                            "thought_signature": thought_signature
                                        }
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            }

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_web",
                    "parameters": {"type": "object"},
                },
            }
        ]
        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            result = llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Research this"}],
                model_name="openai/gpt-5.4-mini",
                tools=tools,
                tool_choice="auto",
            )

        self.assertEqual("", result.content)
        self.assertEqual("tool_calls", result.finish_reason)
        self.assertEqual("search_web", result.tool_calls[0]["function"]["name"])
        self.assertEqual(
            thought_signature,
            result.assistant_message["tool_calls"][0]["extra_content"]["google"][
                "thought_signature"
            ],
        )
        self.assertEqual(tools, captured_payload["tools"])
        self.assertEqual("auto", captured_payload["tool_choice"])

    def test_custom_model_pricing_accounts_for_cached_prompt_tokens(self):
        response = {
            "model": "openai/demo",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 400},
            },
        }
        model = {
            **llm_handler.default_model_record("demo"),
            "input_cost_per_million": 10.0,
            "cached_input_cost_per_million": 2.0,
            "output_cost_per_million": 20.0,
        }
        result = llm_handler._extract_chat_completion_result(
            response, requested_model="openai/demo", model_record=model
        )
        self.assertAlmostEqual(result.cost, 0.0108)
        self.assertEqual(result.cost_source, "custom_model_pricing")
        self.assertEqual(result.usage["cached_prompt_tokens"], 400)
        self.assertEqual(result.usage["uncached_prompt_tokens"], 600)

    def test_chat_completion_with_metadata_uses_model_defaults_when_unset(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Fix this"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={"request_timeout_seconds": 600},
            )

        self.assertNotIn("max_tokens", captured_payload)
        self.assertNotIn("temperature", captured_payload)
        self.assertEqual(captured_payload["timeout"], 600)

    def test_zero_temperature_and_custom_reasoning_are_sent_exactly(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

        provider = next(
            item for item in llm_handler.get_provider_configs(None) if item["id"] == "openai"
        )
        provider["models"] = [
            {
                **llm_handler.default_model_record("gpt-5.4-mini"),
                "default_temperature": 0,
                "default_reasoning_effort": "provider-specific",
            }
        ]
        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "test"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={"provider_configs": [provider]},
            )

        self.assertEqual(captured_payload["temperature"], 0)
        self.assertEqual(captured_payload["reasoning_effort"], "provider-specific")

    def test_task_reasoning_override_wins_over_the_model_default(self):
        captured_payload = {}

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}}]}

        provider = next(
            item
            for item in llm_handler.get_provider_configs(None)
            if item["id"] == "openai"
        )
        provider["models"] = [
            {
                **llm_handler.default_model_record("gpt-5.4-mini"),
                "default_reasoning_effort": "medium",
            }
        ]
        with patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "test"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={
                    "provider_configs": [provider],
                    "reasoning_effort": "high",
                },
            )

        self.assertEqual(captured_payload["reasoning_effort"], "high")

    def test_authoritative_response_cost_wins_over_custom_pricing(self):
        response = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 1000},
            "_hidden_params": {"response_cost": 0.0123},
        }
        model = {
            **llm_handler.default_model_record("demo"),
            "input_cost_per_million": 100.0,
            "output_cost_per_million": 100.0,
        }
        result = llm_handler._extract_chat_completion_result(
            response, requested_model="openai/demo", model_record=model
        )
        self.assertEqual(result.cost, 0.0123)
        self.assertEqual(result.cost_source, "litellm_hidden_params")

    def test_chat_completion_uses_explicit_builtin_provider_api_key(self):
        captured_payload = {}
        provider_configs = llm_handler.get_provider_configs(None)
        for provider in provider_configs:
            if provider["id"] == "openai":
                provider["api_key"] = "explicit-openai-key"

        def fake_completion(**kwargs):
            captured_payload.update(kwargs)
            return {
                "model": kwargs["model"],
                "choices": [{"message": {"content": "ok"}}],
                "_hidden_params": {"response_cost": 0.0},
            }

        with patch.dict(os.environ, {}, clear=True), patch(
            "pandrator.logic.llm_handler._get_litellm_clients",
            return_value=(fake_completion, None),
        ):
            llm_handler.chat_completion_with_metadata(
                messages=[{"role": "user", "content": "Fix this"}],
                model_name="openai/gpt-5.4-mini",
                llm_settings={"provider_configs": provider_configs},
            )

        self.assertEqual(captured_payload["api_key"], "explicit-openai-key")

    def test_validate_model_credentials_requires_builtin_provider_key(self):
        with patch.dict(os.environ, {}, clear=True):
            status = llm_handler.validate_model_credentials(
                "claude-sonnet-4-6",
                {"provider_configs": llm_handler.get_provider_configs(None)},
            )

        self.assertFalse(status.ok)
        self.assertTrue(status.needs_api_key)
        self.assertEqual(status.api_key_env, "ANTHROPIC_API_KEY")
        self.assertIn("Anthropic requires an API key", status.message)

    def test_validate_model_credentials_accepts_builtin_env_key(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "anthropic-key"}, clear=True):
            status = llm_handler.validate_model_credentials(
                "claude-sonnet-4-6",
                {"provider_configs": llm_handler.get_provider_configs(None)},
            )

        self.assertTrue(status.ok)
        self.assertTrue(status.needs_api_key)
        self.assertEqual(status.provider, "anthropic")

    def test_validate_model_credentials_allows_keyless_custom_openai_endpoint(self):
        provider_configs = llm_handler.get_provider_configs(None)
        provider_configs.append(
            {
                "id": "local-openai",
                "name": "Local OpenAI",
                "provider": "openai",
                "api_base": "http://127.0.0.1:8000/v1",
                "api_key": "",
                "api_key_env": "",
                "is_custom": True,
                "models": ["local-model"],
            }
        )

        with patch.dict(os.environ, {}, clear=True):
            status = llm_handler.validate_model_credentials(
                "custom:local-openai/local-model",
                {"provider_configs": provider_configs},
            )

        self.assertTrue(status.ok)
        self.assertFalse(status.needs_api_key)
        self.assertEqual(status.provider_id, "local-openai")

    def test_vertex_credentials_are_forwarded_as_vertex_json_not_api_key(self):
        credentials = '{"type":"service_account","project_id":"fixture"}'
        provider = {
            "id": "vertex",
            "name": "Vertex",
            "provider": "vertex_ai",
            "api_key": "",
            "api_key_env": "",
            "is_custom": True,
            "models": ["gemini-2.5-flash"],
            "request_options": {
                "vertex_credentials": credentials,
                "vertex_project": "fixture",
                "vertex_location": "global",
            },
        }
        details = llm_handler._resolve_model_request_details(
            "custom:vertex/gemini-2.5-flash",
            {"provider_configs": [provider]},
        )
        self.assertEqual(credentials, details["request_overrides"]["vertex_credentials"])
        self.assertNotIn("api_key", details["request_overrides"])
        self.assertTrue(
            llm_handler.validate_model_credentials(
                "custom:vertex/gemini-2.5-flash",
                {"provider_configs": [provider]},
            ).ok
        )

    def test_validate_model_credentials_requires_openrouter_env_for_prefixed_model(self):
        with patch.dict(os.environ, {}, clear=True):
            status = llm_handler.validate_model_credentials(
                "openrouter/deepseek/deepseek-r1",
                {"provider_configs": llm_handler.get_provider_configs(None)},
            )

        self.assertFalse(status.ok)
        self.assertTrue(status.needs_api_key)
        self.assertEqual(status.provider, "openrouter")
        self.assertEqual(status.api_key_env, "OPENROUTER_API_KEY")


if __name__ == "__main__":
    unittest.main()
