import json
import os
import tempfile
import time
import unittest
from threading import Event, Lock
from unittest.mock import patch

from pandrator.logic import dubbing_handler, llm_handler
from pandrator.logic.dubbing import llm_correction, srt_utils


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:01,000
hello

2
00:00:01,100 --> 00:00:02,000
uh

3
00:00:02,100 --> 00:00:04,000
one two
"""


def _settings():
    return {
        "correction_model": "anthropic/claude-sonnet-4-6",
        "original_language": "English",
        "llm_provider_configs": llm_handler.get_provider_configs(None),
        "request_timeout_seconds": 30,
        "reasoning_effort": "",
        "llm_char": 6000,
        "max_subtitles_per_call": 40,
        "max_line_length": 42,
        "context": True,
    }


class DubbingLLMCorrectionTests(unittest.TestCase):
    def test_parse_correction_operations_extracts_fenced_json(self):
        operations = llm_correction.parse_correction_operations(
            """Here is the result:
```json
{"operations":[{"action":"edit","ids":[1],"texts":["Hello."]}]}
```
"""
        )

        self.assertEqual(
            operations,
            [{"action": "edit", "ids": [1], "texts": ["Hello."]}],
        )

    def test_parse_correction_operations_rejects_malformed_entries_instead_of_skipping(self):
        with self.assertRaisesRegex(ValueError, "unsupported action"):
            llm_correction.parse_correction_operations(
                '{"operations":[{"action":"rewrite","ids":[1],"texts":["Hello."]}]}'
            )
        with self.assertRaisesRegex(ValueError, "operations"):
            llm_correction.parse_correction_operations("{}")

    def test_apply_correction_operations_supports_edit_delete_merge_split(self):
        block = [
            {"index": 10, "start": 0.0, "end": 1.0, "text": "hello"},
            {"index": 11, "start": 1.1, "end": 2.0, "text": "world"},
            {"index": 12, "start": 2.1, "end": 4.1, "text": "one two"},
        ]

        corrected = llm_correction.apply_correction_operations(
            block,
            [
                {"action": "merge", "ids": [1, 2], "texts": ["Hello world."]},
                {"action": "split", "ids": [3], "texts": ["One.", "Two."]},
            ],
        )

        self.assertEqual([subtitle["text"] for subtitle in corrected], ["Hello world.", "One.", "Two."])
        self.assertEqual(corrected[0]["start"], 0.0)
        self.assertEqual(corrected[0]["end"], 2.0)
        self.assertEqual(corrected[1]["start"], 2.1)
        self.assertEqual(corrected[2]["end"], 4.1)

    def test_apply_correction_operations_rejects_ambiguous_shapes_and_removes_visual_line_breaks(self):
        block = [
            {"index": 1, "start": 0.0, "end": 1.0, "text": "first"},
            {"index": 2, "start": 1.0, "end": 2.0, "text": "second"},
        ]

        corrected = llm_correction.apply_correction_operations(
            block,
            [
                {"action": "edit", "ids": [1, 2], "texts": ["invalid merge"]},
                {"action": "edit", "ids": [1], "texts": ["First\ncorrected."]},
            ],
        )

        self.assertEqual([subtitle["text"] for subtitle in corrected], ["First corrected.", "second"])

    def test_correction_prompt_delegates_visual_layout_to_finalization(self):
        prompt = llm_correction.build_correction_prompt(
            [{"index": 1, "start": 0.0, "end": 1.0, "text": "hello\nthere"}],
            max_line_length=18,
        )

        self.assertIn("visual wrapping, and line layout are handled by Pandrator", prompt)
        self.assertIn("Do not insert line breaks", prompt)
        self.assertNotIn("max 2 lines", prompt)
        self.assertNotIn('"char_count"', prompt)
        self.assertIn('"text": "hello there"', prompt)

    def test_correction_prompt_marks_overlap_as_non_spoken_evidence(self):
        prompt = llm_correction.build_correction_prompt(
            [
                {
                    "index": 2,
                    "start": 0.8,
                    "end": 1.5,
                    "text": "Okay",
                    "speaker": "Speaker 2",
                    "overlap_with_previous_ms": 200,
                }
            ]
        )
        cue = json.loads(prompt.rsplit("\nThe subtitles:\n", 1)[1])[0]

        self.assertEqual(cue["speaker"], "Speaker 2")
        self.assertEqual(cue["overlap_with_previous_ms"], 200)
        self.assertIn("non-spoken evidence", prompt)

    def test_correction_prompt_can_include_timing_and_gap_policy(self):
        prompt = llm_correction.build_correction_prompt(
            [
                {
                    "index": 2,
                    "start_ms": 3100,
                    "end_ms": 4200,
                    "start": 3.1,
                    "end": 4.2,
                    "text": "A continued thought",
                    "gap_from_previous_ms": 2100,
                }
            ],
            include_timing_context=True,
            substantial_gap_ms=2000,
        )
        cue = json.loads(prompt.rsplit("\nThe subtitles:\n", 1)[1])[0]

        self.assertEqual(3100, cue["start_ms"])
        self.assertEqual(4200, cue["end_ms"])
        self.assertEqual(2100, cue["gap_from_previous_ms"])
        self.assertIn("A gap of 2000 ms or more", prompt)

    def test_structured_speakers_are_non_spoken_prompt_evidence_and_block_cross_speaker_merge(self):
        content = """1
00:00:00,000 --> 00:00:01,000
An unfinished thought,

2
00:00:01,050 --> 00:00:02,000
answered by somebody else.
"""
        prompts = []

        def fake_completion(**kwargs):
            prompts.append(kwargs["messages"][-1]["content"])
            return llm_handler.ChatCompletionResult(
                content='{"operations":[{"action":"merge","ids":[1,2],"texts":["An unfinished thought, answered by somebody else."]}]}'
            )

        result = llm_correction.correct_srt_content(
            content,
            _settings(),
            completion_func=fake_completion,
            speaker_by_subtitle={1: "Speaker 0", 2: "Speaker 1"},
        )

        prompt_cues = json.loads(prompts[0].rsplit("\nThe subtitles:\n", 1)[1])
        self.assertEqual(
            [item["text"] for item in prompt_cues],
            ["An unfinished thought,", "answered by somebody else."],
        )
        self.assertEqual(
            ["Speaker 0", "Speaker 1"],
            [item["speaker"] for item in prompt_cues],
        )
        segments = srt_utils.parse_srt(result.srt_content)
        self.assertEqual(len(segments), 2)
        self.assertNotIn("[SPEAKER_", result.srt_content)

    def test_concurrent_correction_runs_independent_blocks_and_keeps_output_order(self):
        content = """1
00:00:00,000 --> 00:00:01,000
one

2
00:00:01,100 --> 00:00:02,000
two

3
00:00:02,100 --> 00:00:03,000
three
"""
        gate = Event()
        lock = Lock()
        active = 0
        maximum_active = 0
        prompts = []

        def fake_completion(**kwargs):
            nonlocal active, maximum_active
            prompt = kwargs["messages"][-1]["content"]
            cue = json.loads(prompt.rsplit("\nThe subtitles:\n", 1)[1])[0]
            prompts.append(prompt)
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active >= 2:
                    gate.set()
            self.assertTrue(gate.wait(2))
            if cue["text"] == "one":
                time.sleep(0.05)
            with lock:
                active -= 1
            return llm_handler.ChatCompletionResult(
                content=json.dumps(
                    {
                        "operations": [
                            {
                                "action": "edit",
                                "ids": [1],
                                "texts": [cue["text"].upper()],
                            }
                        ]
                    }
                )
            )

        result = llm_correction.correct_srt_content(
            content,
            {
                **_settings(),
                "max_subtitles_per_call": 1,
                "llm_concurrent_calls": 2,
            },
            completion_func=fake_completion,
        )

        self.assertGreaterEqual(maximum_active, 2)
        self.assertTrue(all("Prior corrected cues" not in prompt for prompt in prompts))
        self.assertEqual(
            ["ONE", "TWO", "THREE"],
            [segment.text for segment in srt_utils.parse_srt(result.srt_content)],
        )

    def test_apply_correction_operations_can_prevent_deletion(self):
        block = [
            {"index": 1, "start": 0.0, "end": 1.0, "text": "uh"},
            {"index": 2, "start": 1.0, "end": 2.0, "text": "um"},
        ]

        corrected = llm_correction.apply_correction_operations(
            block,
            [{"action": "delete", "ids": [1, 2], "texts": []}],
            no_remove_subtitles=True,
        )

        self.assertEqual(corrected, block)

    def test_correct_srt_content_uses_pandrator_llm_request_shape(self):
        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            return llm_handler.ChatCompletionResult(
                content='{"operations":[{"action":"edit","ids":[1],"texts":["[SPEAKER_0]: Hello."]},{"action":"delete","ids":[2],"texts":[]}]}',
                cost=0.025,
            )

        settings = {**_settings(), "reasoning_effort": "high"}
        result = llm_correction.correct_srt_content(
            SAMPLE_SRT,
            settings,
            correction_instructions="Keep names unchanged.",
            completion_func=fake_completion,
        )

        segments = srt_utils.parse_srt(result.srt_content)
        self.assertEqual([segment.text for segment in segments], ["Hello.", "one two"])
        self.assertEqual(result.cost, 0.025)
        self.assertEqual(result.response_count, 1)
        self.assertEqual(calls[0]["model_name"], "anthropic/claude-sonnet-4-6")
        self.assertEqual(calls[0]["messages"][0]["role"], "system")
        self.assertIn("Keep names unchanged.", calls[0]["messages"][1]["content"])
        self.assertIn("provider_configs", calls[0]["llm_settings"])
        self.assertEqual(calls[0]["llm_settings"]["request_timeout_seconds"], 600)
        self.assertEqual(calls[0]["llm_settings"]["reasoning_effort"], "high")
        self.assertNotIn("max_tokens", calls[0])
        self.assertNotIn("temperature", calls[0])

    def test_correct_srt_content_honors_max_subtitles_per_call(self):
        srt_content = "\n\n".join(
            f"{index}\n00:00:{index - 1:02d},000 --> 00:00:{index:02d},000\nSubtitle {index}."
            for index in range(1, 6)
        )
        prompt_batch_sizes = []

        def fake_completion(**kwargs):
            prompt = kwargs["messages"][-1]["content"]
            subtitles = json.loads(prompt.rsplit("\nThe subtitles:\n", 1)[1])
            prompt_batch_sizes.append(len(subtitles))
            return llm_handler.ChatCompletionResult(content='{"operations":[]}')

        settings = {
            **_settings(),
            "llm_char": 100_000,
            "max_subtitles_per_call": 2,
        }
        progress_updates = []
        result = llm_correction.correct_srt_content(
            srt_content,
            settings,
            completion_func=fake_completion,
            progress_callback=lambda value, detail=None: progress_updates.append((value, detail)),
        )

        self.assertEqual(prompt_batch_sizes, [2, 2, 1])
        self.assertEqual(result.response_count, 3)
        self.assertEqual(len(srt_utils.parse_srt(result.srt_content)), 5)
        completed = [
            (value, detail)
            for value, detail in progress_updates
            if str(detail).startswith("Corrected ")
        ]
        self.assertEqual([0.4, 0.8, 1.0], [value for value, _detail in completed])
        self.assertEqual("Corrected 5 of 5 subtitles", completed[-1][1])

    def test_correct_srt_content_retries_invalid_response(self):
        responses = iter(
            [
                llm_handler.ChatCompletionResult(content="not JSON", cost=0.01),
                llm_handler.ChatCompletionResult(
                    content='{"operations":[{"action":"edit","ids":[1],"texts":["Hello."]}]}',
                    cost=0.02,
                ),
            ]
        )

        result = llm_correction.correct_srt_content(
            SAMPLE_SRT,
            _settings(),
            completion_func=lambda **_kwargs: next(responses),
        )

        self.assertEqual(result.response_count, 2)
        self.assertEqual(result.cost, 0.03)
        self.assertEqual(srt_utils.parse_srt(result.srt_content)[0].text, "Hello.")

    def test_correct_srt_content_retries_valid_json_with_invalid_operations(self):
        calls = []
        responses = iter(
            [
                llm_handler.ChatCompletionResult(
                    content='{"operations":[{"action":"edit","ids":[99],"texts":["Wrong."]}]}',
                    cost=0.01,
                ),
                llm_handler.ChatCompletionResult(
                    content='{"operations":[{"action":"edit","ids":[1],"texts":["Hello."]}]}',
                    cost=0.02,
                ),
            ]
        )

        def complete(**kwargs):
            calls.append(kwargs)
            return next(responses)

        result = llm_correction.correct_srt_content(
            SAMPLE_SRT,
            _settings(),
            completion_func=complete,
        )

        self.assertEqual(2, result.response_count)
        self.assertAlmostEqual(0.03, result.cost)
        self.assertIn("previous response was rejected", calls[1]["messages"][2]["content"])
        self.assertEqual("Hello.", srt_utils.parse_srt(result.srt_content)[0].text)

    def test_next_batch_context_contains_corrected_cues_not_prior_operations(self):
        settings = {**_settings(), "llm_char": 1}
        prompts = []

        def fake_completion(**kwargs):
            prompts.append(kwargs["messages"][-1]["content"])
            if len(prompts) == 1:
                return llm_handler.ChatCompletionResult(
                    content='{"operations":[{"action":"edit","ids":[1],"texts":["Hello."]}]}',
                )
            return llm_handler.ChatCompletionResult(content='{"operations":[]}')

        llm_correction.correct_srt_content(SAMPLE_SRT, settings, completion_func=fake_completion)

        self.assertGreater(len(prompts), 1)
        context = prompts[1].split("Prior corrected cues", 1)[1].split("The subtitles:", 1)[0]
        self.assertIn('["Hello."]', context)
        self.assertNotIn('"action"', context)

    def test_dubbing_handler_correction_writes_native_result(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "native.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE_SRT)

            with patch(
                "pandrator.logic.dubbing.llm_correction.llm_handler.chat_completion_with_metadata",
                return_value=llm_handler.ChatCompletionResult(
                    content='{"operations":[{"action":"edit","ids":[1],"texts":["Hello."]}]}',
                    cost=0.01,
                ),
            ):
                self.assertTrue(
                    dubbing_handler.correct_subtitles(
                        temp_dir,
                        srt_path,
                        _settings(),
                        correction_prompt="Correct punctuation.",
                    )
                )

            corrected_path = os.path.join(temp_dir, "native_corrected.srt")
            self.assertTrue(os.path.exists(corrected_path))
            with open(corrected_path, "r", encoding="utf-8") as handle:
                self.assertIn("Hello.", handle.read())

    def test_correct_srt_file_with_result_returns_output_path_and_usage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            srt_path = os.path.join(temp_dir, "native.srt")
            with open(srt_path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE_SRT)

            result = llm_correction.correct_srt_file_with_result(
                temp_dir,
                srt_path,
                _settings(),
                completion_func=lambda **_kwargs: llm_handler.ChatCompletionResult(
                    content='{"operations":[{"action":"edit","ids":[1],"texts":["Hello."]}]}',
                    cost=0.03,
                ),
            )

            self.assertTrue(result.output_path.endswith("native_corrected.srt"))
            self.assertTrue(os.path.exists(result.output_path))
            self.assertEqual(result.cost, 0.03)
            self.assertEqual(result.response_count, 1)


if __name__ == "__main__":
    unittest.main()
