import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.logic import dubbing_handler, llm_handler
from pandrator.logic.dubbing import llm_translation, srt_utils


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:01,000
Hello.

2
00:00:01,100 --> 00:00:02,000
Remove this.
"""


def _settings(glossary_enabled=False):
    return {
        "translation_backend": "llm",
        "translation_model": "anthropic/claude-sonnet-4-6",
        "original_language": "English",
        "target_language": "pl",
        "llm_provider_configs": llm_handler.get_provider_configs(None),
        "request_timeout_seconds": 30,
        "reasoning_effort": "",
        "llm_char": 6000,
        "max_subtitles_per_call": 40,
        "context": True,
        "glossary_enabled": glossary_enabled,
    }


class DubbingLLMTranslationTests(unittest.TestCase):
    def test_parse_translation_response_extracts_glossary(self):
        translations, glossary = llm_translation.parse_translation_response(
            """[
  {"number": 1, "text": "Czesc."}
]
[GLOSSARY]
hello = czesc
""",
            expected_count=1,
        )

        self.assertEqual(translations, ["Czesc."])
        self.assertEqual(glossary, {"hello": "czesc"})

    def test_translation_responses_to_srt_removes_marked_subtitles(self):
        srt_content = llm_translation.translation_responses_to_srt(
            [
                {
                    "translation": ["Czesc.", "[REMOVE]"],
                    "original_indices": [1, 2],
                }
            ],
            SAMPLE_SRT,
        )

        segments = srt_utils.parse_srt(srt_content)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].text, "Czesc.")
        self.assertEqual(segments[0].start_ms, 0)
        self.assertEqual(segments[0].end_ms, 1000)

    def test_deepl_language_code_mapping(self):
        self.assertEqual(llm_translation.get_deepl_language_code("English"), "EN-US")
        self.assertEqual(llm_translation.get_deepl_language_code("pt-BR"), "PT-BR")
        self.assertEqual(llm_translation.get_deepl_language_code("Japanese"), "JA")

    def test_translate_blocks_deepl_maps_translated_units_to_original_indices(self):
        class FakeTranslator:
            def __init__(self):
                self.target_langs = []

            def translate_text(self, text, target_lang):
                self.target_langs.append(target_lang)
                return SimpleNamespace(text=text.replace("Hello.", "Czesc.").replace("Remove this.", "Usun to."))

        fake_translator = FakeTranslator()
        responses = llm_translation.translate_blocks_deepl(
            [
                [
                    {"index": 1, "text": "Hello."},
                    {"index": 2, "text": "Remove this."},
                ]
            ],
            "English",
            "pl",
            "test-key",
            translator_factory=lambda auth_key: fake_translator,
        )

        self.assertEqual(fake_translator.target_langs, ["PL"])
        self.assertEqual(responses[0]["translation"], ["Czesc.", "Usun to."])
        self.assertEqual(responses[0]["original_indices"], [1, 2])

    def test_translate_srt_content_uses_pandrator_llm_request_shape(self):
        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            return llm_handler.ChatCompletionResult(
                content='[{"number":1,"text":"Czesc."},{"number":2,"text":"[REMOVE]"}]\n[GLOSSARY]\nhello = czesc',
                cost=0.05,
            )

        result = llm_translation.translate_srt_content(
            SAMPLE_SRT,
            _settings(glossary_enabled=True),
            translation_instructions="Use informal language.",
            glossary={"test": "test"},
            completion_func=fake_completion,
        )

        self.assertIn("Czesc.", result.srt_content)
        self.assertNotIn("Remove this.", result.srt_content)
        self.assertEqual(result.glossary["hello"], "czesc")
        self.assertEqual(result.cost, 0.05)
        self.assertEqual(calls[0]["model_name"], "anthropic/claude-sonnet-4-6")
        self.assertIn("Use informal language.", calls[0]["messages"][0]["content"])
        self.assertIn("provider_configs", calls[0]["llm_settings"])
        self.assertNotIn("max_tokens", calls[0])

    def test_translate_srt_content_honors_max_subtitles_per_call(self):
        srt_content = "\n\n".join(
            f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\nSubtitle {index}."
            for index in range(1, 6)
        )
        prompt_batch_sizes = []

        def fake_completion(**kwargs):
            prompt = kwargs["messages"][0]["content"]
            subtitles = json.loads(prompt.rsplit("\nThe subtitles:\n", 1)[1])
            prompt_batch_sizes.append(len(subtitles))
            return llm_handler.ChatCompletionResult(
                content=json.dumps(
                    [
                        {"number": item["number"], "text": f"Translated {item['text']}"}
                        for item in subtitles
                    ]
                )
            )

        settings = {
            **_settings(),
            "llm_char": 100_000,
            "max_subtitles_per_call": 2,
        }
        result = llm_translation.translate_srt_content(
            srt_content,
            settings,
            completion_func=fake_completion,
        )

        self.assertEqual(prompt_batch_sizes, [2, 2, 1])
        self.assertEqual(result.response_count, 3)
        self.assertEqual(len(srt_utils.parse_srt(result.srt_content)), 5)

    def test_dubbing_handler_translation_writes_native_llm_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "native.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE_SRT)

            with patch(
                "pandrator.logic.dubbing.llm_translation.llm_handler.chat_completion_with_metadata",
                return_value=llm_handler.ChatCompletionResult(
                    content='[{"number":1,"text":"Czesc."},{"number":2,"text":"[REMOVE]"}]',
                    cost=0.01,
                ),
            ):
                self.assertTrue(
                    dubbing_handler.translate_subtitles(
                        temp_dir,
                        srt_path,
                        _settings(),
                    )
                )

            translated_path = os.path.join(temp_dir, "native_pl.srt")
            blocks_path = os.path.join(temp_dir, "native_pl_final_blocks.json")
            self.assertTrue(os.path.exists(translated_path))
            self.assertTrue(os.path.exists(blocks_path))
            with open(blocks_path, "r", encoding="utf-8") as handle:
                blocks = json.load(handle)
            self.assertEqual(blocks[0]["translation"], ["Czesc.", "[REMOVE]"])

    def test_translate_srt_file_with_result_returns_output_path_and_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "native.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE_SRT)

            result = llm_translation.translate_srt_file_with_result(
                temp_dir,
                srt_path,
                _settings(),
                completion_func=lambda **_kwargs: llm_handler.ChatCompletionResult(
                    content='[{"number":1,"text":"Czesc."},{"number":2,"text":"[REMOVE]"}]',
                    cost=0.04,
                ),
            )

            self.assertTrue(result.output_path.endswith("native_pl.srt"))
            self.assertTrue(os.path.exists(result.output_path))
            self.assertEqual(result.cost, 0.04)
            self.assertEqual(result.response_count, 1)

    def test_dubbing_handler_translation_writes_native_deepl_result(self):
        class FakeTranslator:
            def translate_text(self, text, target_lang):
                self.target_lang = target_lang
                return SimpleNamespace(text=text.replace("Hello.", "Czesc.").replace("Remove this.", "Usun to."))

        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "deepl.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE_SRT)

            fake_translator = FakeTranslator()
            deepl_settings = _settings()
            deepl_settings["translation_backend"] = "deepl"

            with patch.dict(os.environ, {"DEEPL_API_KEY": "test-deepl-key"}), patch(
                "pandrator.logic.dubbing.llm_translation._build_deepl_translator",
                return_value=fake_translator,
            ):
                self.assertTrue(
                    dubbing_handler.translate_subtitles(
                        temp_dir,
                        srt_path,
                        deepl_settings,
                    )
                )

            translated_path = os.path.join(temp_dir, "deepl_pl.srt")
            blocks_path = os.path.join(temp_dir, "deepl_pl_final_blocks.json")
            self.assertTrue(os.path.exists(translated_path))
            self.assertTrue(os.path.exists(blocks_path))
            with open(translated_path, "r", encoding="utf-8") as handle:
                translated_srt = handle.read()
            self.assertIn("Czesc.", translated_srt)
            self.assertIn("Usun to.", translated_srt)
            self.assertEqual(fake_translator.target_lang, "PL")


if __name__ == "__main__":
    unittest.main()
