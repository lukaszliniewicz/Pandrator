import unittest
from unittest.mock import patch

from pandrator.logic.dialogue_flow import classify_dialogue_paragraphs
from pandrator.logic.text_preprocessor import preprocess_text


class DialogueFlowScannerTests(unittest.TestCase):
    def test_english_tags_and_quote_coverage_are_conservative(self):
        paragraphs = [
            "“I will return before the last light fades,” said Scrooge.",
            "“I would rather stay here and finish my work,” he replied.",
        ]

        result = classify_dialogue_paragraphs(paragraphs, "en")

        self.assertEqual([item["speech_boundary_after"] for item in result], [
            "dialogue_turn", "paragraph"
        ])
        self.assertTrue(all(item["rule_language"] == "en" for item in result))
        self.assertIsNone(
            classify_dialogue_paragraphs(
                ["“I cannot stay,” then he walked into the night."], "en"
            )[0]
        )
        self.assertIsNone(
            classify_dialogue_paragraphs(
                [
                    "“I have said enough words to cross the paragraph threshold,” "
                    "said Scrooge walked away."
                ],
                "en",
            )[0]
        )
        self.assertIsNone(classify_dialogue_paragraphs(["It's a bright day."], "en")[0])

    def test_short_fully_quoted_replies_count_content_without_delimiters(self):
        english = classify_dialogue_paragraphs(["“Hi”"], "en")
        japanese = classify_dialogue_paragraphs(["「へえ」"], "ja")

        self.assertEqual(english[0]["rule_language"], "en")
        self.assertEqual(japanese[0]["rule_language"], "ja")
        self.assertEqual(english[0]["speech_boundary_after"], "paragraph")
        self.assertEqual(japanese[0]["speech_boundary_after"], "paragraph")
        self.assertEqual(
            classify_dialogue_paragraphs(["“”", "“   ”"], "en"),
            [None, None],
        )
        self.assertEqual(
            classify_dialogue_paragraphs(["「」", "「  」"], "ja"),
            [None, None],
        )

    def test_english_paragraphs_may_contain_multiple_quoted_spans(self):
        paragraphs = [
            "“I do,” said Scrooge. “Merry Christmas! What right have you to be "
            "merry? What reason have you to be merry? You’re poor enough.”",
            "“Christmas a humbug, uncle!” said Scrooge’s nephew. “You don’t mean "
            "that, I am sure?”",
            "“Come, then,” returned the nephew gaily. “What right have you to be "
            "dismal? What reason have you to be morose? You’re rich enough.”",
        ]

        result = classify_dialogue_paragraphs(paragraphs, "en")

        self.assertEqual(
            [item["speech_boundary_after"] for item in result],
            ["dialogue_turn", "dialogue_turn", "paragraph"],
        )
        self.assertEqual(
            classify_dialogue_paragraphs(
                [paragraphs[0] + " “I will return,” then he left the room."],
                "en",
            ),
            [None],
        )

    def test_language_profiles_control_quote_and_dash_detection(self):
        polish = classify_dialogue_paragraphs(
            ["— Nie wiem, co się wydarzyło.", "– Musimy porozmawiać o tym spokojnie."],
            "pl-PL",
        )
        french = classify_dialogue_paragraphs(
            ["— Je ne sais pas ce qui s'est passé.", "– Nous devons en parler calmement."],
            "fr",
        )
        english_dash = classify_dialogue_paragraphs(
            ["—This is not an English dialogue rule.", "—Neither is this."], "en"
        )
        japanese = classify_dialogue_paragraphs(
            ["「これは十分に長い会話の一文です。」"], "ja"
        )
        dutch = classify_dialogue_paragraphs(
            ["‘Dit is een voldoende lange zin om als dialoog te gelden.’"], "nl"
        )

        self.assertEqual(polish[0]["speech_boundary_after"], "dialogue_turn")
        self.assertEqual(polish[1]["speech_boundary_after"], "paragraph")
        self.assertEqual(french[0]["speech_boundary_after"], "dialogue_turn")
        self.assertEqual(french[1]["speech_boundary_after"], "paragraph")
        self.assertEqual(english_dash, [None, None])
        self.assertEqual(japanese[0]["rule_language"], "ja")
        self.assertEqual(dutch[0]["rule_language"], "nl")
        self.assertEqual(
            classify_dialogue_paragraphs(["- first item", "- second item"], "pl"),
            [None, None],
        )

    def test_french_unspaced_em_dash_dialogue_is_language_specific(self):
        verne_examples = [
            ["—Par exemple!", "—Où voulez-vous qu'il aille?"],
            ["—Quoi?", "—D'éteindre le bec de gaz de ma chambre!"],
        ]
        for paragraphs in verne_examples:
            with self.subTest(paragraphs=paragraphs):
                result = classify_dialogue_paragraphs(paragraphs, "fr")
                self.assertEqual(
                    [item["speech_boundary_after"] for item in result],
                    ["dialogue_turn", "paragraph"],
                )

        quoted = classify_dialogue_paragraphs(
            ["—«Par exemple!»", "—“Où allez-vous?”"], "fr"
        )
        self.assertEqual(
            [item["speech_boundary_after"] for item in quoted],
            ["dialogue_turn", "paragraph"],
        )
        self.assertEqual(
            classify_dialogue_paragraphs(["—Par exemple!"], "fr"),
            [None],
        )
        self.assertEqual(
            classify_dialogue_paragraphs(["—2026", "–10"], "fr"),
            [None, None],
        )
        self.assertEqual(
            classify_dialogue_paragraphs(["—Nie wiem!", "—Na pewno?"], "pl"),
            [None, None],
        )
        self.assertEqual(
            classify_dialogue_paragraphs(["-Par exemple!", "-Où allez-vous?"], "fr"),
            [None, None],
        )

    def test_german_quote_styles_and_reporting_tags_are_recognized(self):
        german = classify_dialogue_paragraphs(
            [
                "„Ich komme heute vor Sonnenuntergang zurück“, sagte sie.",
                "„Dann warte ich hier und halte die Tür offen“, antwortete er.",
            ],
            "de",
        )
        inverted = classify_dialogue_paragraphs(
            [
                "»Ich komme heute vor Sonnenuntergang zurück«, sagte sie.",
                "»Dann warte ich hier und halte die Tür offen«, sagte er.",
            ],
            "de",
        )
        action = classify_dialogue_paragraphs(
            ["„Ich komme bald“, dann ging sie weg."], "de"
        )

        self.assertEqual(
            [item["speech_boundary_after"] for item in german],
            ["dialogue_turn", "paragraph"],
        )
        self.assertEqual(
            [item["speech_boundary_after"] for item in inverted],
            ["dialogue_turn", "paragraph"],
        )
        self.assertEqual(action, [None])

    def test_french_opening_guillemet_exchange_closes_before_next_quote_run(self):
        paragraphs = [
            "«Où voulez-vous aller?",
            "—Au port.",
            "—Allons-y.»",
            "“Je reviens demain.”",
            "“Nous vous attendrons.”",
        ]
        classified = classify_dialogue_paragraphs(paragraphs, "fr")

        self.assertEqual(
            [item["speech_boundary_after"] if item else None for item in classified],
            ["dialogue_turn", "dialogue_turn", "paragraph", "dialogue_turn", "paragraph"],
        )

        result = DialogueFlowPreprocessorTests.prepare(
            "\n\n".join(paragraphs), language="fr", max_sentence_length=300
        )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["dialogue_flow"]["paragraph_count"], 3)
        self.assertEqual(result[0]["speech_boundary_after"], "paragraph")
        self.assertLessEqual(len(result[0]["original_sentence"]), 300)
        self.assertEqual(result[1]["dialogue_flow"]["paragraph_count"], 2)

    def test_french_opening_guillemet_requires_a_bounded_contiguous_closing_run(self):
        unclosed = ["«Où voulez-vous aller?", "—Au port."]
        too_long = ["«" + ("x" * 600), "—Au port."]
        interrupted = [
            "«Où voulez-vous aller?",
            "—Au port.",
            "Il s'arrêta pour réfléchir.",
            "—Allons-y.»",
        ]
        for paragraphs in (unclosed, too_long, interrupted):
            with self.subTest(paragraphs=paragraphs):
                result = classify_dialogue_paragraphs(paragraphs, "fr")
                self.assertIsNone(result[0])

        replies_64 = [f"—Réplique {index}." for index in range(1, 64)]
        replies_64.append("—Dernière réplique.»")
        bounded = classify_dialogue_paragraphs(
            ["«Début?", *replies_64], "fr"
        )
        self.assertEqual(bounded[0]["speech_boundary_after"], "dialogue_turn")
        self.assertEqual(bounded[-1]["speech_boundary_after"], "paragraph")

        replies_65 = [f"—Réplique {index}." for index in range(1, 65)]
        replies_65.append("—Dernière réplique.»")
        beyond_bound = classify_dialogue_paragraphs(
            ["«Début?", *replies_65], "fr"
        )
        self.assertIsNone(beyond_bound[0])

    def test_generic_profile_accepts_only_balanced_fully_quoted_paragraphs(self):
        accepted = classify_dialogue_paragraphs(
            ["“A complete quoted sentence with no attribution.”", "「別の引用文です。」"],
            "unknown-language",
        )
        self.assertEqual(accepted[0]["rule_language"], "generic")
        self.assertEqual(accepted[0]["speech_boundary_after"], "dialogue_turn")
        self.assertIsNone(
            classify_dialogue_paragraphs(
                ["“A quoted sentence that is long enough,” said Scrooge."],
                "unknown-language",
            )[0]
        )
        self.assertEqual(
            classify_dialogue_paragraphs(
                ["—A dash-led sentence.", "—Another dash-led sentence."],
                "unknown-language",
            ),
            [None, None],
        )

    def test_unbalanced_inline_and_action_narration_are_not_dialogue(self):
        cases = [
            ("“This quotation never closes.", "en"),
            ("A sentence with an inline “quoted phrase” in the middle.", "en"),
            (
                "“I will be home before dark and bring everything back,” "
                "then he walked into the night.",
                "en",
            ),
            (
                "“This quotation begins here but stays open.\n\n"
                "It closes only in this next paragraph.”",
                "en",
            ),
        ]

        for text, language in cases:
            with self.subTest(text=text):
                self.assertTrue(
                    all(
                        item is None
                        for item in classify_dialogue_paragraphs(
                            text.split("\n\n"), language
                        )
                    )
                )


class DialogueFlowPreprocessorTests(unittest.TestCase):
    @staticmethod
    def prepare(text, language="en", **overrides):
        settings = {
            "language": language,
            "max_sentence_length": 300,
            "enable_nemo_normalization": False,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "audiobook_preparation": True,
            "audiobook_dialogue_flow": "auto",
            "audiobook_dialogue_merge_allowed": True,
            "tts_service": "XTTS",
        }
        settings.update(overrides)
        with patch(
            "pandrator.logic.text_preprocessor.sentence_segmenter.split_text",
            side_effect=lambda value: [value],
        ):
            return preprocess_text(text, settings)

    def test_three_quoted_paragraphs_merge_when_voice_and_budgets_allow(self):
        text = (
            "“I have considered every detail and reached my decision.”\n\n"
            "“Then we should begin the work before the evening ends.”\n\n"
            "“Agreed, I will gather the papers and meet you shortly.”"
        )

        result = self.prepare(text)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["dialogue_flow"]["paragraph_count"], 3)
        self.assertEqual(result[0]["speech_boundary_after"], "paragraph")
        self.assertEqual(result[0]["paragraph"], "yes")
        self.assertIn("\n\n", result[0]["original_sentence"])
        self.assertIn("“", result[0]["original_sentence"])

    def test_dickens_multi_quote_exchange_merges_losslessly_with_large_budget(self):
        text = (
            "“I do,” said Scrooge. “Merry Christmas! What right have you to be "
            "merry? What reason have you to be merry? You’re poor enough.”\n\n"
            "“Christmas a humbug, uncle!” said Scrooge’s nephew. “You don’t mean "
            "that, I am sure?”\n\n"
            "“Come, then,” returned the nephew gaily. “What right have you to be "
            "dismal? What reason have you to be morose? You’re rich enough.”"
        )

        result = self.prepare(text, max_sentence_length=1152)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["dialogue_flow"]["paragraph_count"], 3)
        self.assertEqual(result[0]["speech_boundary_after"], "paragraph")
        self.assertEqual(result[0]["original_sentence"], text)

    def test_japanese_short_replies_merge_when_budget_allows(self):
        text = "「へえ」\n\n「うん」"

        result = self.prepare(text, language="ja", max_sentence_length=32)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["dialogue_flow"]["paragraph_count"], 2)
        self.assertEqual(result[0]["speech_boundary_after"], "paragraph")
        self.assertLessEqual(len(result[0]["original_sentence"]), 32)

    def test_auto_dialogue_uses_sequential_path_across_would_be_chunk_boundary(self):
        first = "“I have checked every record and made my final decision.”"
        second = "“Then let us begin before the evening ends.”"
        text = f"{first}\n\n{second}"
        small_chunk_size = max(len(first), len(second)) + 1
        self.assertGreater(len(text), small_chunk_size)

        settings = {
            "language": "en",
            "max_sentence_length": 300,
            "enable_nemo_normalization": False,
            "enable_sentence_splitting": True,
            "enable_sentence_appending": True,
            "audiobook_preparation": True,
            "audiobook_dialogue_flow": "auto",
            "audiobook_dialogue_merge_allowed": True,
            "tts_service": "XTTS",
        }
        with (
            patch("pandrator.logic.text_preprocessor.CHUNK_SIZE", small_chunk_size),
            patch("pandrator.logic.text_preprocessor.sentence_segmenter.is_available", return_value=False),
            patch(
                "pandrator.logic.text_preprocessor._parallel_preprocess_text",
                side_effect=AssertionError("dialogue context must stay sequential"),
            ) as parallel_preprocess,
            patch(
                "pandrator.logic.text_preprocessor.sentence_segmenter.split_text",
                side_effect=lambda value: [value],
            ),
        ):
            result = preprocess_text(text, settings)

        parallel_preprocess.assert_not_called()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["original_sentence"], text)
        self.assertEqual(result[0]["dialogue_flow"]["paragraph_count"], 2)
        self.assertLessEqual(len(result[0]["original_sentence"]), 300)

    def test_non_auto_large_audiobook_keeps_existing_parallel_choice(self):
        settings = {
            "enable_nemo_normalization": False,
            "audiobook_preparation": True,
            "audiobook_dialogue_flow": "off",
        }
        with (
            patch("pandrator.logic.text_preprocessor.CHUNK_SIZE", 8),
            patch("pandrator.logic.text_preprocessor.sentence_segmenter.is_available", return_value=False),
            patch(
                "pandrator.logic.text_preprocessor._parallel_preprocess_text",
                return_value=[],
            ) as parallel_preprocess,
            patch(
                "pandrator.logic.text_preprocessor._sequential_preprocess_text",
                side_effect=AssertionError("non-auto settings retain parallel choice"),
            ),
        ):
            preprocess_text("long enough", settings)

        parallel_preprocess.assert_called_once()

    def test_voice_separation_and_budget_overflow_keep_turns_separate(self):
        text = (
            "“I have considered every detail and reached my decision.”\n\n"
            "“Then we should begin the work before the evening ends.”"
        )
        separated = self.prepare(text, audiobook_dialogue_merge_allowed=False)
        limited = self.prepare(text, max_sentence_length=80)

        self.assertEqual(len(separated), 2)
        self.assertEqual(separated[0]["speech_boundary_after"], "dialogue_turn")
        self.assertEqual(separated[1]["speech_boundary_after"], "paragraph")
        self.assertEqual(len(limited), 2)
        self.assertTrue(all(len(row["original_sentence"]) <= 80 for row in limited))

    def test_quote_removal_happens_after_dialogue_detection(self):
        text = (
            "“I have considered every detail and reached my decision.”\n\n"
            "“Then we should begin the work before the evening ends.”"
        )

        result = self.prepare(text, remove_quotation_marks=True)

        self.assertEqual(len(result), 1)
        self.assertNotIn("“", result[0]["original_sentence"])
        self.assertEqual(result[0]["dialogue_flow"]["paragraph_count"], 2)

    def test_scene_chapter_and_glyph_paragraphs_block_dialogue_merging(self):
        quoted = "“This is a complete dialogue sentence with enough words.”"
        scene = self.prepare(quoted + "\n\n\n" + quoted)
        chapter = self.prepare(quoted + "\n\n[[Chapter]]A new chapter\n\n" + quoted)
        glyph = self.prepare(quoted + "\n\n✦\n\n" + quoted)

        self.assertEqual(len(scene), 2)
        self.assertTrue(all(row["paragraph"] == "yes" for row in scene))
        self.assertGreaterEqual(len(chapter), 3)
        self.assertEqual(sum(row.get("chapter") == "yes" for row in chapter), 1)
        self.assertFalse(
            any("\n\n" in row["original_sentence"] for row in glyph)
        )

    def test_dialogue_detection_is_off_without_explicit_audiobook_opt_in(self):
        text = "“A complete quoted sentence with no attribution.”"
        with patch(
            "pandrator.logic.text_preprocessor.sentence_segmenter.split_text",
            side_effect=lambda value: [value],
        ):
            ordinary = preprocess_text(
                text,
                {
                    "language": "en",
                    "enable_nemo_normalization": False,
                    "enable_sentence_appending": True,
                    "max_sentence_length": 300,
                },
            )
        self.assertTrue(all("dialogue_flow" not in row for row in ordinary))


if __name__ == "__main__":
    unittest.main()
