"""Focused coverage for generation-segment literal search and filtering."""

from __future__ import annotations

import tempfile
import unittest

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_search import contains_literal, literal_matches
from pandrator.web.models import GenerationSegment


class GenerationSearchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]
        self.generation = self.services["generation"]
        self.database = self.services["database"]
        self.session_id = self.client.post(
            "/api/v1/sessions",
            json={"name": "Generation search", "workflow_kind": "audiobook"},
            headers=self.headers,
        ).get_json()["id"]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def create_plan(self, segments):
        return self.generation.create_plan(
            self.session_id,
            source_revision_id=None,
            segments=segments,
        )

    def list_segments(self, **query):
        return self.client.get(
            f"/api/v1/sessions/{self.session_id}/generation-segments",
            query_string=query,
        )

    def test_literal_search_pages_matches_beyond_default_page(self):
        self.create_plan(
            [{"text": f"Row {index}: marker"} for index in range(300)]
        )

        first = self.list_segments(q="marker", limit=100).get_json()
        self.assertEqual(300, first["total"])
        self.assertEqual(list(range(100)), [item["ordinal"] for item in first["items"]])
        self.assertEqual(100, first["next_cursor"])

        second = self.list_segments(q="marker", limit=100, cursor=100).get_json()
        self.assertEqual(list(range(100, 200)), [item["ordinal"] for item in second["items"]])
        self.assertEqual(200, second["next_cursor"])

        third = self.list_segments(q="marker", limit=100, cursor=200).get_json()
        self.assertEqual(list(range(200, 300)), [item["ordinal"] for item in third["items"]])
        self.assertIsNone(third["next_cursor"])

    def test_search_combines_filters_and_ordinal_pagination(self):
        plan = self.create_plan([{"text": f"Target {index}"} for index in range(12)])
        with self.database.session() as session:
            rows = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == plan["active_revision_id"])
                    .order_by(GenerationSegment.ordinal)
                )
            )
            for row in rows:
                row.marked = row.ordinal % 2 == 0
                row.status = "completed" if row.ordinal % 3 == 0 else "ready"
        response = self.list_segments(
            q="target",
            status="completed",
            marked="true",
            limit=2,
            end_ordinal=9,
        )
        payload = response.get_json()
        self.assertEqual(2, payload["total"])
        self.assertEqual([0, 6], [item["ordinal"] for item in payload["items"]])
        self.assertIsNone(payload["next_cursor"])

        page = self.list_segments(
            q="target",
            status="completed",
            marked="true",
            limit=1,
            cursor=1,
            end_ordinal=9,
        ).get_json()
        self.assertEqual(2, page["total"])
        self.assertEqual([6], [item["ordinal"] for item in page["items"]])

    def test_unicode_whole_word_and_punctuation_matching_is_literal(self):
        self.assertTrue(contains_literal("Straße", "STRASSE"))
        self.assertTrue(contains_literal("un café", "CAFÉ", whole_word=True))
        self.assertFalse(contains_literal("caféteria", "café", whole_word=True))
        self.assertTrue(contains_literal("literal foo% [x]", "foo%"))
        self.assertTrue(contains_literal("literal foo% [x]", "[x]", whole_word=True))
        self.assertTrue(contains_literal("ellipsis...", "...", whole_word=True))

        self.create_plan(
            [
                {"text": "Straße foo% [x]"},
                {"text": "caféteria"},
                {"text": "un CAFÉ"},
            ]
        )
        response = self.list_segments(q="STRASSE", whole_word="true").get_json()
        self.assertEqual([0], [item["ordinal"] for item in response["items"]])
        response = self.list_segments(q="foo%", match_case="true").get_json()
        self.assertEqual([0], [item["ordinal"] for item in response["items"]])
        response = self.list_segments(q="café", whole_word="true").get_json()
        self.assertEqual([2], [item["ordinal"] for item in response["items"]])

    def test_literal_matches_return_utf16_spans_without_cutting_casefold_expansions(self):
        self.assertEqual(
            [{"start": 0, "end": 6}],
            literal_matches("Straße", "STRASSE"),
        )
        self.assertEqual(
            [{"start": 0, "end": 1}],
            literal_matches("İstanbul", "i\u0307"),
        )
        self.assertEqual(
            [{"start": 9, "end": 10}],
            literal_matches("İstanbul i", "i"),
        )
        self.assertEqual([], literal_matches("İstanbul", "i"))
        self.assertEqual(
            [{"start": 0, "end": 2}, {"start": 2, "end": 4}],
            literal_matches("😀😀", "😀"),
        )
        self.assertEqual(
            [{"start": 0, "end": 1}, {"start": 1, "end": 2}],
            literal_matches("ßß", "ss"),
        )
        self.assertEqual([], literal_matches("ß", "s"))
        self.assertEqual(
            [{"start": 2, "end": 5}],
            literal_matches("  foo  ", "foo"),
        )
        self.assertEqual(
            [{"start": 0, "end": 2}],
            literal_matches("e\u0301 e", "e\u0301", whole_word=True),
        )

    def test_rejected_partial_casefold_match_does_not_skip_a_valid_span(self):
        self.assertEqual([{"start": 1, "end": 2}], literal_matches("sß", "ss"))
        self.assertEqual([{"start": 2, "end": 3}], literal_matches("😀ß", "ss"))

    def test_spoken_search_uses_optimized_text_with_fallback(self):
        self.create_plan(
            [
                {"text": "Original one", "optimized_text": "Spoken one"},
                {"text": "Original two"},
            ]
        )
        spoken = self.list_segments(q="original", text_field="spoken").get_json()
        self.assertEqual([1], [item["ordinal"] for item in spoken["items"]])
        optimized = self.list_segments(q="spoken", text_field="spoken").get_json()
        self.assertEqual([0], [item["ordinal"] for item in optimized["items"]])

    def test_boundary_flags_count_is_global_and_filter_is_applied(self):
        self.create_plan(
            [
                {
                    "text": f"Flagged {index}",
                    "speech_block_provenance": {
                        "risk_flags": [{"kind": "boundary"}]
                        if index < 17
                        else None
                    },
                }
                for index in range(20)
            ]
        )
        flagged = self.list_segments(
            q="flagged", boundary_flags="true", limit=5
        ).get_json()
        self.assertEqual(17, flagged["boundary_flag_count"])
        self.assertEqual(17, flagged["total"])
        self.assertEqual(list(range(5)), [item["ordinal"] for item in flagged["items"]])

        unflagged = self.list_segments(
            q="flagged", boundary_flags="false"
        ).get_json()
        self.assertEqual(17, unflagged["boundary_flag_count"])
        self.assertEqual(3, unflagged["total"])
        self.assertEqual([17, 18, 19], [item["ordinal"] for item in unflagged["items"]])

    def test_boundary_flag_filters_exclude_removed_blocks_in_both_directions(self):
        self.create_plan([
            {"text": "Flagged", "speech_block_provenance": {"risk_flags": ["gap"]}},
            {"text": "Unflagged"},
        ])
        with self.database.session() as db:
            for row in db.scalars(select(GenerationSegment)):
                row.removed = True
        for value in ("true", "false"):
            with self.subTest(boundary_flags=value):
                page = self.list_segments(boundary_flags=value).get_json()
                self.assertEqual(0, page["total"])
                self.assertEqual(0, page["boundary_flag_count"])

    def test_invalid_search_parameters_return_validation_error(self):
        self.create_plan([{"text": "one"}])
        cases = (
            {"marked": "yes"},
            {"match_case": "yes"},
            {"whole_word": "1"},
            {"boundary_flags": "no"},
            {"text_field": "optimized"},
            {"q": "x" * 4001},
        )
        for query in cases:
            with self.subTest(query=query):
                response = self.list_segments(**query)
                self.assertEqual(422, response.status_code, response.get_json())

    def test_query_absent_keeps_listing_shape_and_results(self):
        self.create_plan([{"text": "one"}, {"text": "two"}])
        baseline = self.list_segments().get_json()
        repeated = self.list_segments().get_json()
        self.assertEqual(baseline, repeated)
        self.assertEqual(0, baseline["boundary_flag_count"])
        self.assertNotIn("search_matches", baseline["items"][0])

    def test_search_matches_are_returned_in_explicit_projection(self):
        self.create_plan([{"text": "😀 Straße"}])
        payload = self.list_segments(
            q="strasse",
            fields="id,ordinal,revision,text,optimized_text,search_matches",
        ).get_json()
        self.assertEqual(
            [{"start": 3, "end": 9}], payload["items"][0]["search_matches"]
        )


if __name__ == "__main__":
    unittest.main()
