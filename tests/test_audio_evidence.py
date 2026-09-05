import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator.logic import llm_handler
from pandrator.logic.audio_evidence import (
    MAX_INLINE_AUDIO_BYTES,
    transcribe_audio_evidence,
)


class AudioEvidenceTests(unittest.TestCase):
    def _audio_file(
        self, directory: str, suffix: str = ".wav", data: bytes = b"audio"
    ) -> Path:
        path = Path(directory) / f"sample{suffix}"
        path.write_bytes(data)
        return path

    def _completion(self, *, content: str = "Transcript", usage: dict | None = None):
        return llm_handler.ChatCompletionResult(
            content=content,
            model="openai/test-model",
            usage={} if usage is None else usage,
        )

    def test_builds_exact_chat_audio_parts_with_raw_base64(self):
        audio_bytes = b"RIFF\x00\x01 audio fixture"
        with tempfile.TemporaryDirectory() as directory:
            path = self._audio_file(directory, data=audio_bytes)
            completion = self._completion(content="```text\n  Hello, world!  \n```")
            with patch(
                "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata",
                return_value=completion,
            ) as complete:
                result = transcribe_audio_evidence(
                    path,
                    "Transcribe this clip.",
                    "openai/gpt-5.4-mini",
                    {"request_timeout_seconds": 30},
                )

        complete.assert_called_once()
        self.assertEqual("Hello, world!", result.transcript)
        self.assertIs(result.completion, completion)
        messages = complete.call_args.kwargs["messages"]
        self.assertEqual(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Transcribe this clip."},
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": base64.b64encode(audio_bytes).decode("ascii"),
                                "format": "wav",
                            },
                        },
                    ],
                }
            ],
            messages,
        )
        self.assertFalse(
            messages[0]["content"][1]["input_audio"]["data"].startswith("data:")
        )

    def test_mp3_format_and_provider_wire_mappings(self):
        cases = (
            ("gemini", False, "gemini_generate_content.inlineData"),
            ("vertex_ai", False, "vertex_generate_content.inlineData"),
            ("openai", False, "openai_chat_completions.input_audio"),
            ("gemini", True, "openai_chat_completions.input_audio"),
            ("custom-provider", True, "openai_chat_completions.input_audio"),
        )
        with tempfile.TemporaryDirectory() as directory:
            path = self._audio_file(directory, suffix=".mp3")
            for provider_key, is_custom, expected_mapping in cases:
                with self.subTest(provider_key=provider_key, is_custom=is_custom):
                    completion = self._completion(usage={"nested": {"audio_tokens": 2}})
                    with patch(
                        "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata",
                        return_value=completion,
                    ) as complete:
                        result = transcribe_audio_evidence(
                            path,
                            "Transcribe.",
                            "gemini/test-model",
                            provider_key=provider_key,
                            is_custom=is_custom,
                        )

                    self.assertEqual(
                        "mp3",
                        complete.call_args.kwargs["messages"][0]["content"][1][
                            "input_audio"
                        ]["format"],
                    )
                    self.assertEqual(
                        expected_mapping,
                        result.transport_metadata["provider_wire_mapping"],
                    )

    def test_forwards_cancellation_and_retry_callback(self):
        cancel_event = object()
        retry_callback = object()
        with tempfile.TemporaryDirectory() as directory:
            path = self._audio_file(directory)
            completion = self._completion(usage={"audio_tokens": 1})
            with patch(
                "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata",
                return_value=completion,
            ) as complete:
                transcribe_audio_evidence(
                    path,
                    "Transcribe.",
                    "openai/test-model",
                    cancel_event=cancel_event,
                    retry_callback=retry_callback,
                )

        self.assertIs(cancel_event, complete.call_args.kwargs["cancel_event"])
        self.assertIs(retry_callback, complete.call_args.kwargs["retry_callback"])

    def test_rejects_unsupported_empty_and_oversized_audio_before_call(self):
        with tempfile.TemporaryDirectory() as directory:
            unsupported = self._audio_file(directory, suffix=".flac")
            empty = self._audio_file(directory, suffix=".wav", data=b"")
            oversized = self._audio_file(
                directory,
                suffix=".mp3",
                data=b"x" * (MAX_INLINE_AUDIO_BYTES + 1),
            )
            with patch(
                "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata"
            ) as complete:
                with self.assertRaisesRegex(ValueError, "only .wav and .mp3"):
                    transcribe_audio_evidence(unsupported, "Prompt", "openai/model")
                with self.assertRaisesRegex(ValueError, "empty"):
                    transcribe_audio_evidence(empty, "Prompt", "openai/model")
                with self.assertRaisesRegex(ValueError, "18 MiB"):
                    transcribe_audio_evidence(oversized, "Prompt", "openai/model")

        complete.assert_not_called()

    def test_audio_token_usage_states(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._audio_file(directory)
            for usage, expected in (
                ({"audio_tokens": 12}, "confirmed"),
                (
                    {
                        "cached_input_details": {"audio_tokens": 0},
                        "input_details": {"audio_tokens": 12},
                    },
                    "confirmed",
                ),
                ({"details": [{"audio_tokens": 0}]}, "zero"),
                ({"prompt_tokens": 4}, "unreported"),
            ):
                with self.subTest(expected=expected):
                    completion = self._completion(usage=usage)
                    with patch(
                        "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata",
                        return_value=completion,
                    ):
                        if expected == "zero":
                            with self.assertRaisesRegex(
                                RuntimeError, "zero audio tokens"
                            ):
                                transcribe_audio_evidence(
                                    path, "Prompt", "openai/model"
                                )
                        else:
                            result = transcribe_audio_evidence(
                                path, "Prompt", "openai/model"
                            )
                            self.assertEqual(expected, result.audio_consumption)

    def test_rejects_empty_provider_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._audio_file(directory)
            completion = self._completion(content=" \n ```\n``` ")
            with (
                patch(
                    "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata",
                    return_value=completion,
                ),
                self.assertRaisesRegex(RuntimeError, "empty content"),
            ):
                transcribe_audio_evidence(path, "Prompt", "openai/model")

    def test_transport_metadata_contains_no_audio_or_secret_material(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self._audio_file(directory, data=b"secret-audio")
            completion = self._completion(usage={"audio_tokens": 1})
            with patch(
                "pandrator.logic.audio_evidence.llm_handler.chat_completion_with_metadata",
                return_value=completion,
            ):
                result = transcribe_audio_evidence(
                    path,
                    "Prompt",
                    "custom:provider/secret-model",
                    provider_key="custom-provider",
                    is_custom=True,
                )

        metadata_text = repr(result.transport_metadata)
        self.assertNotIn(
            base64.b64encode(b"secret-audio").decode("ascii"), metadata_text
        )
        self.assertNotIn("secret-model", metadata_text)
        self.assertNotIn("secret", metadata_text)
        self.assertEqual(
            "openai_chat_completions.input_audio",
            result.transport_metadata["input_contract"],
        )


if __name__ == "__main__":
    unittest.main()
