import unittest
from concurrent.futures import Future
from unittest.mock import MagicMock, call, patch

from pandrator.logic.text_preprocessor import (
    CHUNK_SIZE,
    _ensure_line_terminal_punctuation,
    _get_sentence_splitter,
    _parallel_preprocess_text,
    _split_with_sentence_splitter,
    append_short_sentences,
    find_best_split_index,
    preprocess_text,
    split_into_sentences,
    split_long_sentences_2,
)


class TextPreprocessorTests(unittest.TestCase):
    def test_fallback_reuses_language_prefixes_without_changing_splits(self):
        from sentence_splitter import SentenceSplitter

        examples = {
            "en": ["Dr. Smith waited. Then he left.", "A different paragraph. More text."],
            "pl": ["To jest tekst. A to następne zdanie.", "Inny akapit. I jeszcze jeden."],
            "fr": ["Mme. Martin attend. Puis elle part.", "Un autre paragraphe. Encore du texte."],
            "nl": ["Dit is tekst. Nog een zin.", "Een andere alinea. Meer tekst."],
            "de": ["Dr. Schmidt wartet. Dann geht er.", "Ein anderer Absatz. Noch mehr Text."],
        }
        expected = {
            language: [SentenceSplitter(language=language).split(text) for text in texts]
            for language, texts in examples.items()
        }
        _get_sentence_splitter.cache_clear()
        try:
            with patch(
                "pandrator.logic.text_preprocessor.SentenceSplitter", wraps=SentenceSplitter
            ) as constructor:
                for language, texts in examples.items():
                    actual = [_split_with_sentence_splitter(text, language) for text in texts]
                    self.assertEqual(actual, expected[language])
                self.assertEqual(constructor.call_count, len(examples))
        finally:
            _get_sentence_splitter.cache_clear()

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_single_newlines_are_wrapping_and_blank_lines_are_paragraphs(self, split_text):
        split_text.side_effect = [
            ["First sentence.", "Second sentence."],
            ["New paragraph."],
        ]
        settings = {
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text("First sentence.\nSecond sentence.\n\nNew paragraph.", settings)

        self.assertEqual(
            split_text.call_args_list,
            [call("First sentence. Second sentence."), call("New paragraph.")],
        )
        self.assertEqual([item["paragraph"] for item in sentences], ["no", "yes", "yes"])

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_sentence_appending_does_not_cross_source_paragraphs(self, split_text):
        split_text.side_effect = [["First dialogue."], ["Second dialogue."]]
        settings = {
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text("First dialogue.\n\nSecond dialogue.", settings)

        self.assertEqual(
            [item["original_sentence"] for item in sentences],
            ["First dialogue.", "Second dialogue."],
        )
        self.assertEqual([item["paragraph"] for item in sentences], ["yes", "yes"])

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_wtpsplit_is_primary_sentence_segmenter(self, split_text):
        split_text.return_value = ["See Sec. IV, Ch. IX, and pp. 12-14.", "Then continue."]

        result = split_into_sentences(
            "See Sec. IV, Ch. IX, and pp. 12-14. Then continue.",
            "en",
            "XTTS",
        )

        self.assertEqual(result, split_text.return_value)

    @patch("pandrator.logic.text_preprocessor._split_with_sentence_splitter")
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text", return_value=None)
    def test_rule_based_segmenter_is_used_as_wtpsplit_fallback(self, _split_text, legacy_split):
        legacy_split.return_value = ["One.", "Two."]

        result = split_into_sentences("One. Two.", "en", "XTTS")

        self.assertEqual(result, ["One.", "Two."])
        legacy_split.assert_called_once_with("One. Two.", "en")

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_sentence_packing_rejoins_neural_clause_cuts_before_testing_fit(self, split_text):
        first = "A lantern warmed the small kitchen while rain tapped on the old windows through the quiet evening."
        left_clause = (
            "The travelers followed a narrow track through the valley, carrying provisions and maps "
            "as the last light faded and the evening wind grew cold;"
        )
        right_clause = (
            "after sunset they reached a village beside the river, where warm windows shone above "
            "the old stone bridge and a friendly host prepared a room."
        )
        last = "The river passed beneath the stone bridge and disappeared beyond the fields."
        complete_sentence = left_clause + " " + right_clause
        self.assertLessEqual(len(complete_sentence), 350)
        self.assertGreater(len(first + " " + complete_sentence), 350)
        self.assertGreater(len(complete_sentence + " " + last), 350)
        split_text.return_value = [first, left_clause, right_clause, last]
        source = " ".join(split_text.return_value)
        for policy, threshold, isolated in (
            ("auto", 350, True),
            ("auto", 351, False),
            ("sentences", 900, True),
            ("clauses", 1, False),
        ):
            with self.subTest(policy=policy, threshold=threshold):
                rows = preprocess_text(source, {
                    "language": "en",
                    "tts_service": "XTTS",
                    "audiobook_preparation": True,
                    "max_sentence_length": 350,
                    "enable_sentence_appending": True,
                    "enable_nemo_normalization": False,
                    "normalize_all_caps": False,
                    "audiobook_sentence_packing": policy,
                    "audiobook_sentence_packing_threshold": threshold,
                })
                blocks = [row["original_sentence"] for row in rows]
                self.assertEqual(source, " ".join(blocks))
                self.assertTrue(all(len(block) <= 350 for block in blocks))
                if isolated:
                    self.assertEqual([first, complete_sentence, last], blocks)
                else:
                    self.assertEqual([first + " " + left_clause, right_clause + " " + last], blocks)

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_whole_sentence_mode_respects_cjk_terminal_quotes_without_appending(self, split_text):
        split_text.return_value = ["「確かに、", "次の句です。」", "別の文です。"]
        rows = preprocess_text("「確かに、次の句です。」別の文です。", {
            "language": "ja",
            "audiobook_preparation": True,
            "audiobook_sentence_packing": "sentences",
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "max_sentence_length": 350,
        })
        self.assertEqual(
            [row["original_sentence"] for row in rows],
            ["「確かに、次の句です。」", "別の文です。"],
        )

    @patch("pandrator.logic.text_preprocessor._sequential_preprocess_text", return_value=[])
    @patch("pandrator.logic.text_preprocessor._parallel_preprocess_text")
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.is_available", return_value=True)
    def test_large_documents_avoid_loading_wtpsplit_in_worker_processes(
        self,
        _is_available,
        parallel_preprocess,
        sequential_preprocess,
    ):
        settings = {"enable_nemo_normalization": False}

        preprocess_text("a" * (CHUNK_SIZE + 1), settings)

        sequential_preprocess.assert_called_once()
        parallel_preprocess.assert_not_called()

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_preparation_preserves_unicode_punctuation(self, split_text):
        split_text.side_effect = lambda text: [text]
        examples = {
            "fr": "« L’esprit s’ouvre à l’expérience… »",
            "en": "“It’s a test”—with a dash, and pages 2–4.",
            "de": "„Das ist’s – wirklich!“",
            "ja": "「意識とは何でしょうか？」",
            "zh": "“什么是意识？”",
        }
        for language, text in examples.items():
            with self.subTest(language=language):
                result = preprocess_text(text, {
                    "language": language,
                    "enable_nemo_normalization": False,
                    "enable_sentence_splitting": False,
                    "enable_sentence_appending": False,
                    "remove_diacritics": False,
                    "remove_quotation_marks": False,
                    "normalize_all_caps": False,
                    "max_sentence_length": 200,
                    "tts_service": "audio_cpp",
                })
                self.assertEqual([row["original_sentence"] for row in result], [text])

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_explicit_quote_removal_preserves_french_apostrophes(self, split_text):
        split_text.side_effect = lambda text: [text]
        result = preprocess_text("« L’esprit s’ouvre à l’expérience. »", {
            "language": "fr",
            "enable_nemo_normalization": False,
            "enable_sentence_splitting": False,
            "enable_sentence_appending": False,
            "remove_quotation_marks": True,
            "max_sentence_length": 200,
            "tts_service": "audio_cpp",
        })
        self.assertEqual(result[0]["original_sentence"], "L’esprit s’ouvre à l’expérience.")

    def test_terminal_punctuation_before_nested_closing_marks(self):
        text = 'He asked “Stop!』'
        self.assertEqual(_ensure_line_terminal_punctuation(text), text)
        self.assertEqual(
            _ensure_line_terminal_punctuation('He asked “Stop”』'),
            'He asked “Stop”』.',
        )

    def test_preprocessor_pipeline_integration(self):
        settings = {
            "pdf_preprocessed": False,
            "source_file": "",
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "remove_diacritics": False,
            "remove_quotation_marks": False,
            "tts_service": "XTTS"
        }
        text = "This is a test—with a dash. And “fancy quotes”."
        sentences = preprocess_text(text, settings)
        
        self.assertEqual(len(sentences), 2)
        self.assertEqual(sentences[0]["original_sentence"], "This is a test—with a dash.")
        self.assertEqual(sentences[1]["original_sentence"], 'And “fancy quotes”.')

    @patch("pandrator.logic.text_preprocessor.nemo_normalizer.normalize_text_for_tts")
    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_nemo_normalization_runs_before_sentence_splitting(self, split_text, normalize_text):
        normalize_text.return_value = "It costs twelve dollars. Continue."
        split_text.return_value = ["It costs twelve dollars.", "Continue."]
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": True,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text("It costs $12. Continue.", settings)

        normalize_text.assert_called_once_with("It costs $12. Continue.", "en")
        split_text.assert_called_once_with("It costs twelve dollars. Continue.")
        self.assertEqual(sentences[0]["original_sentence"], "It costs twelve dollars.")

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_chapter_markers_are_not_sent_to_sentence_segmenter(self, split_text):
        seen_texts = []

        def fake_split(text):
            seen_texts.append(text)
            return [text.strip()]

        split_text.side_effect = fake_split
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text(
            "[[Chapter]]By CHARLES DICKENS\n\nThis is body text.",
            settings,
        )

        self.assertTrue(seen_texts)
        self.assertTrue(all("[[Chapter]]" not in text for text in seen_texts))
        self.assertEqual(sentences[0]["original_sentence"], "By CHARLES DICKENS.")
        self.assertEqual(sentences[0]["chapter"], "yes")
        self.assertEqual(sentences[1]["original_sentence"], "This is body text.")
        self.assertEqual(sentences[1]["chapter"], "no")

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_japanese_terminal_punctuation_does_not_get_ascii_period(self, split_text):
        seen_texts = []

        def fake_split(text):
            seen_texts.append(text)
            return [text.strip()]

        split_text.side_effect = fake_split
        settings = {
            "disable_paragraph_detection": True,
            "language": "ja",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "Kokoro",
        }

        preprocess_text("これはテストです。\n\n次の行です！\n\n質問です？\n\n全角です．", settings)

        self.assertEqual(
            seen_texts,
            ["これはテストです。", "次の行です！", "質問です？", "全角です．"],
        )
        self.assertTrue(all("。." not in text for text in seen_texts))
        self.assertTrue(all("！." not in text for text in seen_texts))
        self.assertTrue(all("？." not in text for text in seen_texts))
        self.assertTrue(all("．." not in text for text in seen_texts))

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_adjacent_chapter_markers_merge_without_segmenter_damage(self, split_text):
        split_text.return_value = ["Body starts here."]
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text(
            "[[Chapter]]STAVE ONE\n\n[[Chapter]]MARLEY'S GHOST\n\nBody starts here.",
            settings,
        )

        self.assertEqual(sentences[0]["original_sentence"], "STAVE ONE. MARLEY'S GHOST.")
        self.assertEqual(sentences[0]["chapter"], "yes")
        split_text.assert_called_once_with("Body starts here.")

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_sentence_before_chapter_marker_keeps_paragraph_boundary(self, split_text):
        split_text.side_effect = [
            ["The last sentence of the previous chapter."],
            ["The first sentence of the next chapter."],
        ]
        settings = {
            "disable_paragraph_detection": True,
            "language": "en",
            "max_sentence_length": 200,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": False,
            "enable_nemo_normalization": False,
            "tts_service": "XTTS",
        }

        sentences = preprocess_text(
            "The last sentence of the previous chapter.\n\n"
            "[[Chapter]]STAVE TWO\n\n"
            "The first sentence of the next chapter.",
            settings,
        )

        self.assertEqual(sentences[0]["original_sentence"], "The last sentence of the previous chapter.")
        self.assertEqual(sentences[0]["paragraph"], "yes")
        self.assertEqual(sentences[1]["original_sentence"], "STAVE TWO.")
        self.assertEqual(sentences[1]["chapter"], "yes")

    def test_punctuation_split_keeps_closing_quote_with_previous_part(self):
        text = (
            'This long passage keeps building pressure inside a quoted clause with enough words '
            'to require a split and then Scrooge answered "Never," and the sentence continues '
            'with more material after the quotation so the second part should not begin with a quote.'
        )

        for opening, closing in [('"', '"'), ('“', '”'), ('„', '“')]:
            quoted = text.replace('"Never,"', f'{opening}Never,{closing}')
            with self.subTest(closing=closing):
                split_index = find_best_split_index(quoted, "en", 160)
                self.assertIsNotNone(split_index)
                self.assertNotEqual(quoted[split_index], closing)
                self.assertTrue(quoted[:split_index].rstrip().endswith(',' + closing))

    def test_conjunction_fallback_chooses_balanced_split(self):
        text = (
            "This is a long clause without commas and it keeps going because the author wanted "
            "the rhythm to continue and the fallback should probably find a conjunction or another "
            "soft boundary before falling back to an arbitrary word boundary near the hard limit."
        )

        split_index = find_best_split_index(text, "en", 200)

        self.assertIsNotNone(split_index)
        self.assertGreater(split_index, 80)
        self.assertLessEqual(split_index, 200)

    @patch("pandrator.logic.text_preprocessor.sentence_segmenter.split_text")
    def test_long_sentence_marks_only_internal_splits_as_clause_boundaries(self, split_text):
        sentence = (
            "This long opening clause carries enough detail to need a split, and the second clause "
            "continues with still more narration before the complete sentence finally comes to rest."
        )
        split_text.return_value = [sentence]

        parts = preprocess_text(
            sentence,
            {
                "language": "en",
                "max_sentence_length": 80,
                "enable_sentence_splitting": True,
                "enable_sentence_appending": False,
                "enable_nemo_normalization": False,
                "tts_service": "XTTS",
            },
        )

        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part["sentence_continues_after"] for part in parts[:-1]))
        self.assertFalse(parts[-1]["sentence_continues_after"])

    def test_narration_append_preserves_opening_quote_space(self):
        rows = [
            {
                "original_sentence": "nephew.",
                "paragraph": "no",
                "chapter": "no",
                "sentence_continues_after": False,
            },
            {
                "original_sentence": '"You are welcome."',
                "paragraph": "no",
                "chapter": "no",
                "sentence_continues_after": False,
            },
        ]

        result = append_short_sentences(rows, 200)

        self.assertEqual(result[0]["original_sentence"], 'nephew. "You are welcome."')

        rows[1]["original_sentence"] = "‘You are welcome.’"
        result = append_short_sentences(rows, 200)

        self.assertEqual(result[0]["original_sentence"], "nephew. ‘You are welcome.’")

    def test_narration_append_keeps_closing_quote_with_previous_text(self):
        rows = [
            {
                "original_sentence": 'He answered "Humbug!',
                "paragraph": "no",
                "chapter": "no",
                "sentence_continues_after": False,
            },
            {
                "original_sentence": '"',
                "paragraph": "no",
                "chapter": "no",
                "sentence_continues_after": False,
            },
        ]

        result = append_short_sentences(rows, 200)

        self.assertEqual(result[0]["original_sentence"], 'He answered "Humbug!"')

    def test_narration_append_preserves_cjk_seam(self):
        rows = [
            {"original_sentence": "これは", "paragraph": "no", "chapter": "no"},
            {"original_sentence": "テストです。", "paragraph": "no", "chapter": "no"},
        ]

        result = append_short_sentences(rows, 200)

        self.assertEqual(result[0]["original_sentence"], "これはテストです。")

class ParallelPreprocessorContractTests(unittest.TestCase):
    def test_out_of_order_completion_retains_source_order_and_skips_empty_chunks(self):
        chunks = ["first", "empty", "last"]
        rows = [{"original_sentence": "First."}, {"original_sentence": "Last."}]
        futures = [Future() for _ in chunks]
        for future, result in zip(futures, ([rows[0]], [], [rows[1]]), strict=True):
            future.set_result(result)
        executor = MagicMock()
        executor.__enter__.return_value = executor
        executor.submit.side_effect = futures
        progress = MagicMock()
        with (
            patch("pandrator.logic.text_preprocessor._split_text_into_chunks", return_value=chunks),
            patch(
                "pandrator.logic.text_preprocessor.concurrent.futures.ProcessPoolExecutor",
                return_value=executor,
            ),
            patch(
                "pandrator.logic.text_preprocessor.concurrent.futures.as_completed",
                return_value=reversed(futures),
            ),
        ):
            result = _parallel_preprocess_text("fixture", {}, progress_callback=progress)
        self.assertEqual(
            [
                {"original_sentence": "First.", "sentence_number": "1"},
                {"original_sentence": "Last.", "sentence_number": "2"},
            ],
            result,
        )
        self.assertEqual(3, executor.submit.call_count)
        values = [call.args[0] for call in progress.call_args_list]
        self.assertEqual(sorted(values), values)
        self.assertEqual(3, len(values))
        self.assertAlmostEqual(0.9, values[-1])

    def test_worker_failure_propagates_before_returning_partial_narration(self):
        first, failed = Future(), Future()
        first.set_result([{"original_sentence": "First."}])
        error = ValueError("worker failed")
        failed.set_exception(error)
        executor = MagicMock()
        executor.__enter__.return_value = executor
        executor.submit.side_effect = [first, failed]
        with (
            patch(
                "pandrator.logic.text_preprocessor._split_text_into_chunks",
                return_value=["first", "failed"],
            ),
            patch(
                "pandrator.logic.text_preprocessor.concurrent.futures.ProcessPoolExecutor",
                return_value=executor,
            ),
            patch(
                "pandrator.logic.text_preprocessor.concurrent.futures.as_completed",
                return_value=[first, failed],
            ),
        ):
            with self.assertRaises(ValueError) as raised:
                _parallel_preprocess_text("fixture", {})
        self.assertIs(error, raised.exception)
        executor.__exit__.assert_called_once()


class AudiobookSegmentationTests(unittest.TestCase):
    @staticmethod
    def row(text, paragraph="no", **extra):
        row = {
            "original_sentence": text,
            "paragraph": paragraph,
            "chapter": "no",
            "split_part": None,
            "sentence_continues_after": False,
        }
        row.update(extra)
        return row

    def test_greedy_packing_accepts_exact_limit_and_starts_overflow_next(self):
        rows = [
            self.row("One."),
            self.row("Two."),
            self.row("Three."),
            self.row("Four.", paragraph="yes"),
        ]

        packed = append_short_sentences(
            rows, 16, audiobook_preparation=True
        )

        self.assertEqual(
            [row["original_sentence"] for row in packed],
            ["One. Two. Three.", "Four."],
        )
        self.assertEqual([row["paragraph"] for row in packed], ["no", "yes"])

        exact = append_short_sentences(
            [self.row("One."), self.row("Four.", paragraph="yes")],
            10,
            audiobook_preparation=True,
        )
        self.assertEqual([row["original_sentence"] for row in exact], ["One. Four."])

    def test_preserving_sentence_boundaries_isolates_every_oversized_sentence_part(self):
        first = "A lantern warmed the small kitchen while rain tapped on the old windows through the quiet evening."
        clause_one = (
            "The travelers followed a narrow track through the valley, carrying provisions and maps while "
            "the last light faded behind the hills, and they continued carefully along the winding road "
            "as the evening air grew colder"
        )
        clause_two = (
            "After sunset they reached a village beside the river, where warm windows shone above the "
            "stone bridge and a welcoming host prepared a room before the heavy clouds gathered over the fields"
        )
        oversized = f"{clause_one}; {clause_two}."
        last = "The river passed beneath the stone bridge and disappeared beyond the fields."
        self.assertGreater(len(oversized), 350)

        parts = split_long_sentences_2(
            self.row(oversized), 350, "en", audiobook_preparation=True
        )
        rows = [self.row(first), *parts, self.row(last, paragraph="yes")]
        preserved = append_short_sentences(
            rows,
            350,
            preserve_sentence_boundaries=True,
            audiobook_preparation=True,
        )

        self.assertEqual([row["original_sentence"] for row in preserved], [
            first, *(part["original_sentence"] for part in parts), last,
        ])
        self.assertTrue(all(row.get("split_part") is None for row in (preserved[0], preserved[-1])))
        self.assertTrue(all(row.get("split_part") is not None for row in preserved[1:-1]))
        self.assertTrue(all(len(row["original_sentence"]) <= 350 for row in preserved))
        self.assertEqual(["no"] * (len(preserved) - 1) + ["yes"], [
            row["paragraph"] for row in preserved
        ])
        self.assertEqual(
            " ".join(" ".join(row["original_sentence"] for row in preserved).split()),
            " ".join(" ".join([first, oversized, last]).split()),
        )

        compact = append_short_sentences(
            rows, 350, audiobook_preparation=True
        )
        self.assertEqual(len(compact), 2)
        self.assertTrue(compact[0]["original_sentence"].startswith(first))
        self.assertTrue(compact[0]["original_sentence"].endswith(parts[0]["original_sentence"]))
        self.assertTrue(compact[1]["original_sentence"].startswith(parts[-1]["original_sentence"]))
        self.assertTrue(compact[1]["original_sentence"].endswith(last))

    def test_preserving_sentence_boundaries_keeps_word_and_byte_split_parts_isolated(self):
        from pandrator.logic.text_preprocessor import (
            _limit_segment_tokenizer_bytes,
            _limit_segment_words,
        )

        first = "A lantern warmed the small kitchen while rain tapped on the old windows through the quiet evening."
        oversized = (
            "The travelers followed a narrow track through the valley, carrying provisions and maps while "
            "the last light faded behind the hills, and they continued carefully along the winding road "
            "as the evening air grew colder; after sunset they reached a village beside the river, where "
            "warm windows shone above the stone bridge and a welcoming host prepared a room before the "
            "heavy clouds gathered over the fields."
        )
        last = "The river passed beneath the stone bridge and disappeared beyond the fields."
        source = self.row(oversized)

        for parts, word_limit, byte_limit in (
            (_limit_segment_words(source, 20, "en", audiobook_preparation=True), 20, None),
            (_limit_segment_tokenizer_bytes(source, 145, "en", audiobook_preparation=True), None, 145),
        ):
            with self.subTest(parts=len(parts), word_limit=word_limit, byte_limit=byte_limit):
                self.assertGreater(len(parts), 1)
                rows = [self.row(first), *parts, self.row(last, paragraph="yes")]
                packed = append_short_sentences(
                    rows,
                    350,
                    max_segment_words=word_limit,
                    max_segment_tokenizer_bytes=byte_limit,
                    preserve_sentence_boundaries=True,
                    audiobook_preparation=True,
                )
                self.assertEqual(
                    [row["original_sentence"] for row in packed],
                    [first, *(part["original_sentence"] for part in parts), last],
                )
                self.assertTrue(all(row.get("split_part") is not None for row in packed[1:-1]))
                self.assertTrue(all(len(row["original_sentence"]) <= 350 for row in packed))
                if word_limit is not None:
                    self.assertTrue(
                        all(len(row["original_sentence"].split()) <= word_limit for row in packed)
                    )
                if byte_limit is not None:
                    self.assertTrue(all(
                        sum(
                            3 if character == " " else len(character.encode("utf-8"))
                            for character in row["original_sentence"]
                        ) <= byte_limit
                        for row in packed
                    ))

    def test_preserving_mode_still_packs_complete_sentences_with_all_budgets(self):
        rows = [
            self.row("One two."),
            self.row("Three four."),
            self.row("Five six."),
            self.row("Seven eight.", paragraph="yes"),
        ]
        packed = append_short_sentences(
            rows,
            30,
            max_segment_words=4,
            max_segment_tokenizer_bytes=40,
            preserve_sentence_boundaries=True,
            audiobook_preparation=True,
        )
        self.assertEqual(
            [row["original_sentence"] for row in packed],
            ["One two. Three four.", "Five six. Seven eight."],
        )
        self.assertEqual(["no", "yes"], [row["paragraph"] for row in packed])

    def test_preserving_mode_keeps_split_dialogue_parts_out_of_cross_paragraph_merge(self):
        dialogue_metadata = {"source": "punctuation", "paragraph_count": 1}
        rows = [
            self.row(
                "The split dialogue tail.",
                paragraph="yes",
                split_part="0b",
                dialogue_flow=dialogue_metadata.copy(),
                speech_boundary_after="dialogue_turn",
            ),
            self.row(
                "The next complete dialogue turn.",
                paragraph="yes",
                dialogue_flow=dialogue_metadata.copy(),
                speech_boundary_after="paragraph",
            ),
        ]
        packed = append_short_sentences(
            rows,
            200,
            dialogue_merge_allowed=True,
            preserve_sentence_boundaries=True,
            audiobook_preparation=True,
        )
        self.assertEqual(len(packed), 2)
        self.assertEqual(packed[0]["split_part"], "0b")
        self.assertEqual(packed[0]["speech_boundary_after"], "dialogue_turn")
        self.assertEqual(packed[1]["speech_boundary_after"], "paragraph")

    def test_packing_respects_word_and_tokenizer_byte_budgets(self):
        rows = [
            self.row("Élan starts."),
            self.row("A second sentence.", paragraph="yes"),
        ]

        by_words = append_short_sentences(
            rows,
            200,
            max_segment_words=3,
            audiobook_preparation=True,
        )
        self.assertEqual(len(by_words), 2)
        self.assertTrue(
            all(len(row["original_sentence"].split()) <= 3 for row in by_words)
        )

        by_bytes = append_short_sentences(
            rows,
            200,
            max_segment_tokenizer_bytes=24,
            audiobook_preparation=True,
        )
        self.assertEqual(len(by_bytes), 2)

        from pandrator.logic.text_preprocessor import (
            _limit_segment_tokenizer_bytes,
            _limit_segment_words,
        )

        source = self.row(
            "Élan carried several important letters through the old city.",
            paragraph="yes",
        )
        word_parts = _limit_segment_words(
            source, 4, "en", audiobook_preparation=True
        )
        byte_parts = _limit_segment_tokenizer_bytes(
            source, 24, "en", audiobook_preparation=True
        )
        self.assertTrue(
            all(len(part["original_sentence"].split()) <= 4 for part in word_parts)
        )
        self.assertTrue(
            all(
                sum(
                    3 if character == " " else len(character.encode("utf-8"))
                    for character in part["original_sentence"]
                )
                <= 24
                for part in byte_parts
            )
        )
        self.assertEqual(
            " ".join(part["original_sentence"] for part in word_parts),
            "Élan carried several important letters through the old city.",
        )

    def test_dialogue_merge_requires_opt_in_metadata_and_all_budgets(self):
        dialogue_metadata = {"source": "punctuation", "paragraph_count": 1}
        rows = [
            self.row(
                "The first speaker gives a long answer.",
                paragraph="yes",
                dialogue_flow=dialogue_metadata.copy(),
                speech_boundary_after="dialogue_turn",
            ),
            self.row(
                "The second speaker gives another answer.",
                paragraph="yes",
                dialogue_flow=dialogue_metadata.copy(),
                speech_boundary_after="paragraph",
            ),
        ]

        kept = append_short_sentences(
            rows, 200, dialogue_merge_allowed=False, audiobook_preparation=True
        )
        self.assertEqual(len(kept), 2)

        merged = append_short_sentences(
            rows,
            200,
            dialogue_merge_allowed=True,
            audiobook_preparation=True,
        )
        self.assertEqual(len(merged), 1)
        self.assertIn("\n\n", merged[0]["original_sentence"])
        self.assertEqual(merged[0]["dialogue_flow"]["paragraph_count"], 2)
        self.assertEqual(merged[0]["speech_boundary_after"], "paragraph")
        self.assertEqual(merged[0]["paragraph"], "yes")

        too_many_words = append_short_sentences(
            rows,
            200,
            max_segment_words=10,
            dialogue_merge_allowed=True,
            audiobook_preparation=True,
        )
        self.assertEqual(len(too_many_words), 2)

    def test_audiobook_split_prefers_clause_punctuation_and_balanced_whitespace(self):
        sentence = "a" * 49 + ";" + "b" * 49
        split_index = find_best_split_index(
            sentence, "en", 80, audiobook_preparation=True
        )
        self.assertEqual(split_index, 50)

        punctuation_before_nearby_space = "a" * 68 + "; " + "b" * 50
        self.assertEqual(
            find_best_split_index(
                punctuation_before_nearby_space,
                "en",
                80,
                audiobook_preparation=True,
            ),
            69,
        )

        whitespace_only = "alpha " * 14 + "omega"
        parts = split_long_sentences_2(
            self.row(whitespace_only, paragraph="yes"),
            40,
            "en",
            audiobook_preparation=True,
        )
        self.assertTrue(all(len(part["original_sentence"]) <= 40 for part in parts))
        self.assertTrue(all(len(part["original_sentence"]) >= 20 for part in parts))

    def test_oversized_sentence_splits_losslessly_and_terminal_flags_stay_final(self):
        sentence = (
            "The first clause is long enough to speak clearly, and the second clause "
            "continues the idea; the final clause closes it with care."
        )
        source = self.row(
            sentence,
            paragraph="yes",
            dialogue_flow={"source": "punctuation", "paragraph_count": 1},
            speech_boundary_after="paragraph",
        )

        parts = split_long_sentences_2(
            source, 48, "en", audiobook_preparation=True
        )

        self.assertGreater(len(parts), 2)
        self.assertTrue(all(0 < len(part["original_sentence"]) <= 48 for part in parts))
        self.assertEqual(
            " ".join(part["original_sentence"] for part in parts),
            " ".join(sentence.split()),
        )
        self.assertTrue(all(part["paragraph"] == "no" for part in parts[:-1]))
        self.assertTrue(all(part["sentence_continues_after"] for part in parts[:-1]))
        self.assertTrue(all("speech_boundary_after" not in part for part in parts[:-1]))
        self.assertTrue(all("dialogue_flow" not in part for part in parts[:-1]))
        self.assertEqual(parts[-1]["paragraph"], "yes")
        self.assertEqual(parts[-1]["speech_boundary_after"], "paragraph")
        self.assertEqual(parts[-1]["dialogue_flow"]["paragraph_count"], 1)

    def test_hard_character_fallback_is_limited_to_single_long_tokens(self):
        one_token = split_long_sentences_2(
            self.row("unbreakabletoken"),
            5,
            "en",
            audiobook_preparation=True,
        )
        self.assertEqual(
            [part["original_sentence"] for part in one_token],
            ["unbre", "akabl", "etoke", "n"],
        )

        with self.assertRaisesRegex(ValueError, "safe whitespace boundary"):
            split_long_sentences_2(
                self.row("unbreakabletoken followed by words"),
                5,
                "en",
                audiobook_preparation=True,
            )
        with self.assertRaisesRegex(ValueError, "CJK narration exceeds"):
            split_long_sentences_2(
                self.row("これは句読点のない文章です"),
                5,
                "ja",
                audiobook_preparation=True,
            )

    def test_numeric_commas_are_not_clause_boundaries_and_quotes_stay_with_punctuation(self):
        from pandrator.logic.text_preprocessor import (
            _extend_split_index_past_closing_punctuation,
            _is_numeric_comma_boundary,
        )

        self.assertTrue(_is_numeric_comma_boundary("1,234", 2, "comma"))
        self.assertEqual(
            _extend_split_index_past_closing_punctuation('“Stop!” next', 6), 7
        )

if __name__ == "__main__":
    unittest.main()
