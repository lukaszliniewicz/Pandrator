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
                messages[-1]["content"].split(
                    "Plan this single speech sentence:\n", 1
                )[1]
            )
            return json.dumps(
                {
                    "case_id": payload["case_id"],
                    "decisions": [
                        _decision_for(item)
                        for item in payload["unresolved_candidates"]
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
            item
            for item in result.plan["decisions"]
            if item["action"] == "pronounce"
        )
        self.assertEqual("ee-mah-oh-kah", pronunciation["spoken"])

    def test_flexible_plan_with_broken_placeholders_falls_back_to_guarded(self):
        attempted_modes = []

        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split(
                    "Plan this single speech sentence:\n", 1
                )[1]
            )
            flexible = "contextual speech-text editor" in messages[0]["content"]
            attempted_modes.append("flexible" if flexible else "guarded")
            response = {
                "case_id": payload["case_id"],
                "decisions": [
                    _decision_for(item)
                    for item in payload["unresolved_candidates"]
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

    def test_reviewed_pronunciation_is_compiled_without_becoming_model_work(self):
        seen_payload = {}

        def complete(*, messages, **_kwargs):
            payload = json.loads(
                messages[-1]["content"].split(
                    "Plan this single speech sentence:\n", 1
                )[1]
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
        self.assertEqual("eemahohkah arrived.", result.text)
        self.assertEqual("entry-1", result.plan["known_pronunciations"][0]["entry_id"])
        self.assertEqual(3, result.plan["known_pronunciations"][0]["entry_revision"])


if __name__ == "__main__":
    unittest.main()
