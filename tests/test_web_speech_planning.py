import json
import threading
import unittest
from types import SimpleNamespace

from pandrator.web.speech_planning import detect_candidates, plan_speech_text


def _decision_for(candidate):
    text = str(candidate["text"])
    if candidate["suggested_task"] == "pronunciation":
        return {
            "span_id": candidate["id"],
            "action": "pronounce",
            "spoken": "ee-mah-oh-kah",
            "confidence": "high",
        }
    verbalizations = {
        "Dr.": "Doctor",
        "20,000": "twenty thousand",
    }
    return {
        "span_id": candidate["id"],
        "action": "verbalize",
        "spoken": verbalizations.get(text, text),
        "confidence": "high",
    }


class SpeechPlanningTests(unittest.TestCase):
    def test_imaoka_is_detected_and_guarded_plan_compiles_structured_respelling(self):
        candidates, _known = detect_candidates(
            "Dr. Imaoka paid 20,000 francs.",
            language="en",
            known_pronunciations=[],
        )
        self.assertIn(
            "Imaoka",
            [item["text"] for item in candidates if item["task"] == "pronunciation"],
        )

        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": [
                        _decision_for(item) for item in payload["unresolved_candidates"]
                    ],
                    "discoveries": [],
                    "prosody": [],
                }
            )

        result = plan_speech_text(
            "Dr. Imaoka paid 20,000 francs.",
            language="en",
            voice_language="en",
            mode="guarded",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            cancel_event=threading.Event(),
            completion_func=complete,
        )

        self.assertEqual(
            "Doctor eemahohkah paid twenty thousand francs.",
            result.text,
        )
        self.assertEqual("valid", result.plan["status"])
        self.assertEqual("guarded", result.plan["mode_used"])
        pronunciation = next(
            item for item in result.plan["decisions"] if item["action"] == "pronounce"
        )
        self.assertEqual("ee-mah-oh-kah", pronunciation["spoken"])

    def test_flexible_plan_with_broken_placeholders_falls_back_to_guarded(self):
        attempted_modes = []

        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            flexible = "contextual speech-text editor" in messages[0]["content"]
            attempted_modes.append("flexible" if flexible else "guarded")
            response = {
                "case_id": payload["case_id"],
                "decisions": [
                    _decision_for(item) for item in payload["unresolved_candidates"]
                ],
                "discoveries": [],
                "prosody": [],
            }
            if flexible:
                response["speech_template"] = "The model removed every placeholder."
            return json.dumps(response)

        result = plan_speech_text(
            "Imaoka arrived.",
            language="en",
            voice_language="en",
            mode="flexible",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            completion_func=complete,
        )

        self.assertEqual(["flexible", "guarded"], attempted_modes)
        self.assertEqual("guarded", result.plan["mode_used"])
        self.assertEqual("eemahohkah arrived.", result.text)
        self.assertFalse(result.plan["attempts"][0]["valid"])
        self.assertTrue(result.plan["attempts"][1]["valid"])

    def test_guarded_plan_retries_failed_deterministic_validation(self):
        calls = []

        def complete(*, messages, **_kwargs):
            calls.append(messages)
            payload = json.loads(
                messages[1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            if len(calls) == 1:
                return "{}"
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": [
                        _decision_for(item) for item in payload["unresolved_candidates"]
                    ],
                    "discoveries": [],
                    "prosody": [],
                }
            )

        result = plan_speech_text(
            "Imaoka arrived.",
            language="en",
            voice_language="en",
            mode="guarded",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            max_attempts_per_mode=2,
            completion_func=complete,
        )

        self.assertEqual("valid", result.plan["status"])
        self.assertEqual(2, len(result.plan["attempts"]))
        self.assertIn("failed deterministic validation", calls[1][2]["content"])

    def test_reviewed_pronunciation_is_compiled_without_becoming_model_work(self):
        seen_payload = {}

        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            seen_payload.update(payload)
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": [],
                    "discoveries": [],
                    "prosody": [],
                }
            )

        result = plan_speech_text(
            "Imaoka arrived.",
            language="en",
            voice_language="en",
            mode="guarded",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            known_pronunciations=[
                {
                    "id": "entry-1",
                    "revision": 3,
                    "source_form": "Imaoka",
                    "phonetic": "ee-mah-oh-kah",
                }
            ],
            completion_func=complete,
        )

        self.assertEqual([], seen_payload["unresolved_candidates"])
        self.assertEqual(
            [
                {
                    "id": "K1",
                    "text": "Imaoka",
                    "spoken": "ee-mah-oh-kah",
                }
            ],
            seen_payload["reviewed_pronunciations"],
        )
        self.assertEqual("eemahohkah arrived.", result.text)
        self.assertEqual("entry-1", result.plan["known_pronunciations"][0]["entry_id"])
        self.assertEqual(3, result.plan["known_pronunciations"][0]["entry_revision"])

    def test_unicode_pronunciation_decision_is_validated_and_compiled(self):
        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            decisions = []
            for item in payload["unresolved_candidates"]:
                decisions.append(
                    {
                        "span_id": item["id"],
                        "action": "pronounce",
                        "spoken": "łys-kon-syn"
                        if item["text"] == "Imaoka"
                        else "ee-mah-oh-kah",
                        "confidence": "high",
                    }
                )
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": decisions,
                    "discoveries": [],
                    "prosody": [],
                }
            )

        result = plan_speech_text(
            "We visited Imaoka.",
            language="en",
            voice_language="en",
            mode="guarded",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            completion_func=complete,
        )

        self.assertEqual("We visited łyskonsyn.", result.text)
        self.assertEqual("valid", result.plan["status"])

    def test_unicode_pronunciation_discovery_is_validated_and_compiled(self):
        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": [],
                    "discoveries": [
                        {
                            "start_token_id": "T3",
                            "end_token_id": "T3",
                            "source_text": "hello",
                            "action": "pronounce",
                            "spoken": "łys-kon-syn",
                            "confidence": "high",
                        }
                    ],
                    "prosody": [],
                }
            )

        result = plan_speech_text(
            "We saw hello.",
            language="en",
            voice_language="en",
            mode="guarded",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            completion_func=complete,
        )

        self.assertEqual("We saw łyskonsyn.", result.text)
        self.assertEqual("valid", result.plan["status"])

    def test_model_rejects_uncased_or_unsafe_pronunciation_output(self):
        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split("Plan this single speech sentence:\n", 1)[
                    1
                ]
            )
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": [
                        {
                            "span_id": item["id"],
                            "action": "pronounce",
                            "spoken": "Łys-kon-syn"
                            if item["text"] == "Imaoka"
                            else "łys--kon",
                            "confidence": "high",
                        }
                        for item in payload["unresolved_candidates"]
                    ],
                    "discoveries": [],
                    "prosody": [],
                }
            )

        result = plan_speech_text(
            "We visited Imaoka.",
            language="en",
            voice_language="en",
            mode="guarded",
            model_name="local/test",
            llm_settings=SimpleNamespace(),
            completion_func=complete,
        )

        self.assertEqual("We visited Imaoka.", result.text)
        self.assertEqual("safe_fallback", result.plan["status"])
        self.assertFalse(result.plan["attempts"][0]["valid"])
        self.assertTrue(
            any(
                "invalid pronunciation format" in error
                for error in result.plan["attempts"][0]["errors"]
            )
        )


if __name__ == "__main__":
    unittest.main()
