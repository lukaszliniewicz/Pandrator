import io
import os
import tempfile
import unittest
from pathlib import Path

from pandrator.logic import llm_handler
from pandrator.logic.dubbing import equalization, languages, srt_utils, video_muxing, zoom


SAMPLE_SRT = """7
00:00:01,000 --> 00:00:02,000
[SPEAKER_00]: I think

9
00:00:02,050 --> 00:00:02,500
[SPEAKER_00]: so

10
00:00:03,000 --> 00:00:04,000
[SPEAKER_01]: No.
"""


class DubbingSubtitleLogicTests(unittest.TestCase):
    def test_parse_and_renumber_srt(self):
        segments = srt_utils.parse_srt(SAMPLE_SRT)

        self.assertEqual([segment.index for segment in segments], [7, 9, 10])
        self.assertEqual(segments[0].start_ms, 1000)
        self.assertEqual(segments[1].end_ms, 2500)

        renumbered = srt_utils.renumber_subtitles(SAMPLE_SRT)

        self.assertIn("1\n00:00:01,000 --> 00:00:02,000", renumbered)
        self.assertIn("2\n00:00:02,050 --> 00:00:02,500", renumbered)
        self.assertIn("3\n00:00:03,000 --> 00:00:04,000", renumbered)

    def test_merge_subtitles_respects_speaker_labels_and_timing(self):
        merged_srt, diarization_detected = srt_utils.merge_subtitles_with_speaker_awareness(
            SAMPLE_SRT,
            merge_threshold=250,
        )

        self.assertTrue(diarization_detected)
        merged_segments = srt_utils.parse_srt(merged_srt)
        self.assertEqual(len(merged_segments), 2)
        self.assertEqual(merged_segments[0].text, "[SPEAKER_00]: I think so")
        self.assertEqual(merged_segments[0].end_ms, 2500)
        self.assertEqual(merged_segments[1].text, "[SPEAKER_01]: No.")

    def test_merge_subtitles_keeps_different_speakers_separate(self):
        srt_content = """1
00:00:01,000 --> 00:00:02,000
[SPEAKER_00]: I think

2
00:00:02,050 --> 00:00:02,500
[SPEAKER_01]: so
"""

        merged_srt, diarization_detected = srt_utils.merge_subtitles_with_speaker_awareness(
            srt_content,
            merge_threshold=250,
        )

        self.assertTrue(diarization_detected)
        self.assertEqual(len(srt_utils.parse_srt(merged_srt)), 2)

    def test_remove_speaker_labels(self):
        cleaned = srt_utils.remove_speaker_labels(SAMPLE_SRT)

        self.assertNotIn("[SPEAKER_", cleaned)
        self.assertIn("I think", cleaned)
        self.assertIn("No.", cleaned)

    def test_create_translation_blocks_prefers_sentence_boundaries(self):
        blocks = srt_utils.create_translation_blocks(
            """1
00:00:00,000 --> 00:00:01,000
First sentence.

2
00:00:01,200 --> 00:00:02,000
Second sentence continues

3
00:00:02,200 --> 00:00:03,000
with more words.
""",
            char_limit=45,
            source_language="English",
        )

        self.assertEqual(len(blocks), 2)
        self.assertEqual([item["index"] for item in blocks[0]], [1])
        self.assertEqual([item["index"] for item in blocks[1]], [2, 3])

    def test_zoom_vtt_parse_group_and_chunk(self):
        vtt = io.StringIO(
            """WEBVTT

1
00:00:01.000 --> 00:00:02.000
Alice: Hello there.

2
00:00:02.100 --> 00:00:03.000
Alice: More detail.

3
00:00:04.000 --> 00:00:05.000
Bob: Reply.
"""
        )

        utterances = zoom.parse_zoom_vtt(vtt)
        grouped = zoom.group_zoom_utterances(utterances)
        chunks = zoom.create_transcript_chunks_from_grouped(grouped, char_limit=35)

        self.assertEqual(
            utterances,
            [
                {"speaker": "Alice", "text": "Hello there."},
                {"speaker": "Alice", "text": "More detail."},
                {"speaker": "Bob", "text": "Reply."},
            ],
        )
        self.assertEqual(
            grouped,
            [
                {"speaker": "Alice", "text": "Hello there. More detail."},
                {"speaker": "Bob", "text": "Reply."},
            ],
        )
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[0].startswith("Alice:"))

    def test_correct_zoom_vtt_content_uses_pandrator_llm_request_shape(self):
        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            return llm_handler.ChatCompletionResult(
                content="Alice:\nHello there.\n\nBob:\nReply.",
                cost=0.02,
            )

        result = zoom.correct_zoom_vtt_content(
            """WEBVTT

00:00:01.000 --> 00:00:02.000
Alice: hello there

00:00:03.000 --> 00:00:04.000
Bob: reply
""",
            {
                "correction_model": "anthropic/claude-sonnet-4-6",
                "llm_provider_configs": llm_handler.get_provider_configs(None),
                "llm_char": 6000,
            },
            completion_func=fake_completion,
        )

        self.assertEqual(result.transcript_text, "Alice:\nHello there.\n\nBob:\nReply.")
        self.assertEqual(result.cost, 0.02)
        self.assertEqual(result.response_count, 1)
        self.assertEqual(calls[0]["model_name"], "anthropic/claude-sonnet-4-6")
        self.assertIn("Preserve the speaker labels", calls[0]["messages"][0]["content"])

    def test_correct_zoom_vtt_content_uses_default_llm_when_dubbing_provider_is_deepl(self):
        calls = []

        def fake_completion(**kwargs):
            calls.append(kwargs)
            return llm_handler.ChatCompletionResult(content="Alice:\nHello.")

        result = zoom.correct_zoom_vtt_content(
            """WEBVTT

00:00:01.000 --> 00:00:02.000
Alice: hello
""",
            {
                "translation_backend": "deepl",
                "correction_model": "default",
                "llm_default_model": "openai/gpt-5.4-mini",
            },
            completion_func=fake_completion,
        )

        self.assertEqual(result.transcript_text, "Alice:\nHello.")
        self.assertEqual(calls[0]["model_name"], "default")
        self.assertEqual(
            calls[0]["llm_settings"]["default_model"],
            "openai/gpt-5.4-mini",
        )

    def test_correct_zoom_vtt_file_writes_corrected_transcript(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            vtt_path = os.path.join(temp_dir, "meeting.vtt")
            Path(vtt_path).write_text(
                """WEBVTT

00:00:01.000 --> 00:00:02.000
Alice: hello
""",
                encoding="utf-8",
            )

            result = zoom.correct_zoom_vtt_file(
                vtt_path,
                temp_dir,
                {
                    "correction_model": "anthropic/claude-sonnet-4-6",
                },
                completion_func=lambda **_kwargs: llm_handler.ChatCompletionResult(content="Alice:\nHello."),
            )

            self.assertTrue(result.output_path.endswith("meeting_corrected_transcript.txt"))
            self.assertEqual(Path(result.output_path).read_text(encoding="utf-8"), "Alice:\nHello.")

    def test_equalize_subtitle_text_wraps_long_lines(self):
        equalized = equalization.equalize_subtitle_text(
            "This subtitle line is too long and should be wrapped near a word boundary.",
            max_line_length=38,
            max_lines=2,
        )

        lines = equalized.splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(
            " ".join(lines),
            "This subtitle line is too long and should be wrapped near a word boundary.",
        )
        self.assertLessEqual(len(lines[0]), 38)

    def test_ffmpeg_subtitle_language_code_maps_target_language(self):
        self.assertEqual(languages.ffmpeg_subtitle_language_code("pl"), "pol")
        self.assertEqual(languages.ffmpeg_subtitle_language_code("Japanese"), "jpn")
        self.assertEqual(languages.ffmpeg_subtitle_language_code(""), "eng")

    def test_build_soft_subtitle_command_uses_language_metadata(self):
        command = video_muxing.build_add_subtitles_command(
            synced_video_path="video.mp4",
            equalized_srt_path="subs.srt",
            temp_output_path="out.mp4",
            subtitle_mode="soft",
            subtitle_language="pl",
        )

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("mov_text", command)
        self.assertIn("language=pol", command)
        self.assertEqual(command[-1], "out.mp4")

    def test_build_burned_subtitle_command_escapes_filter_path(self):
        command = video_muxing.build_add_subtitles_command(
            synced_video_path="video.mp4",
            equalized_srt_path=r"C:\tmp\name,with[chars].srt",
            temp_output_path="out.mp4",
            subtitle_mode="burned",
            ffmpeg_executable="ffmpeg-libass",
        )

        self.assertEqual(command[0], "ffmpeg-libass")
        self.assertIn("-vf", command)
        filter_arg = command[command.index("-vf") + 1]
        self.assertIn("subtitles=filename=", filter_arg)
        self.assertIn(r"\,", filter_arg)
        self.assertIn(r"\[", filter_arg)
        self.assertIn(r"\]", filter_arg)

    def test_build_replace_video_audio_command_maps_video_and_audio_streams(self):
        command = video_muxing.build_replace_video_audio_command(
            video_path="video.mp4",
            audio_path="dub.wav",
            temp_output_path="out.mp4",
            ffmpeg_executable="ffmpeg-custom",
        )

        self.assertEqual(command[0], "ffmpeg-custom")
        self.assertEqual(command[command.index("-map") + 1], "0:v:0")
        self.assertIn("1:a:0", command)
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertIn("-shortest", command)
        self.assertEqual(command[-1], "out.mp4")


if __name__ == "__main__":
    unittest.main()
