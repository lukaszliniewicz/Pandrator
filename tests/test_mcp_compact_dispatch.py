import copy
import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.compact_packets import compact_manifest_packet
from pandrator_mcp.schemas import ClaimDispatchBatchInput, SubmitDispatchBatchInput
from pandrator_mcp.tools.dispatch import (
    _claim as project_claim,
)
from pandrator_mcp.tools.dispatch import (
    _compact_claim,
    claim_dispatch_batch,
    submit_dispatch_batch,
)

_CATALOG_LANGUAGES = (
    "ar", "bg", "ca", "cs", "da", "de", "el", "en", "es", "et", "fi",
    "fr", "he", "hi", "hr", "hu", "id", "it", "ja", "ko", "lt", "lv",
    "ms", "nl", "no", "pl", "pt", "ro", "ru", "sk", "sl",
)
_CATALOG_REGIONS = ("US", "GB", "CA", "AU", "IN", "BR", "DE", "JP")
_CATALOG_LOCALES = [
    f"{language}-{region}"
    for language in _CATALOG_LANGUAGES
    for region in _CATALOG_REGIONS
]


def _claim_payload(cue_count: int = 40) -> dict:
    cues = []
    for cue_id in range(1, cue_count + 1):
        cue = {
            "cue_id": cue_id,
            "evidence_cue_ids": [cue_id * 10, cue_id * 10 + 1],
            "text": f"Cue {cue_id} keeps its complete source wording.",
            "speaker": "Speaker A" if cue_id % 2 else "Speaker B",
            "turn_id": "turn-a" if cue_id < cue_count // 2 else "turn-b",
            "timing": {
                "start_ms": cue_id * 1_100,
                "end_ms": cue_id * 1_100 + 950,
                "gap_from_previous_ms": 150 if cue_id > 1 else 0,
                "overlap_with_previous_ms": 25 if cue_id > 1 and cue_id % 3 == 0 else 0,
            },
        }
        if cue_id == 1:
            cue["timing_basis"] = "source_passage_window"
        elif cue_id == 2:
            cue["timing"]["timing_basis"] = "source_word_split"
        cues.append(cue)
    return {
        "schema_version": "1",
        "run_id": "run-compact-1",
        "batch_id": "batch-compact-1",
        "batch_ordinal": 1,
        "status": "leased",
        "lease_token": "lease-capability",
        "lease_expires_at": "2030-01-01T00:00:00+00:00",
        "task": {
            "session_id": "session-1",
            "kind": "correction",
            "output_role": "subtitles",
            "source_artifact_id": "artifact-1",
            "source_content_hash": "a" * 64,
            "source_language": "en",
            "target_language": None,
            "instructions": "Preserve all wording unless correction is needed.",
            "result_contract": {"kind": "correction", "version": 1},
            "no_remove_subtitles": True,
            "correction_style": "faithful",
            "known_speakers": ["Speaker A", "Speaker B", "New Speaker"],
            "glossary": {"old term": "preferred term"},
            "timing_context_mode": "full",
            "substantial_gap_ms": 2_000,
            "quality_policy": {
                "preserve_uncertain_text": True,
                "semantic_coherence_required": True,
                "deletion_allowed": False,
                "evidence_tool": "pandrator_request_subtitle_evidence",
                "evidence_status_tool": "pandrator_get_subtitle_evidence",
                "available_routes": ["whisper", "audio_llm"],
                "route_catalog": [
                    {
                        "route": "whisper",
                        "label": "Whisper",
                        "local": True,
                        "remote": False,
                        "timing_method": "native_word",
                        "supported_languages": list(_CATALOG_LOCALES),
                        "language_supported": True,
                        "ready": True,
                        "reason": "Local runtime is available.",
                        "language_reason": "",
                        "route_extension": "preserve this unknown property",
                    },
                    {
                        "route": "audio_llm",
                        "label": "Audio-capable LLM",
                        "local": None,
                        "remote": None,
                        "timing_method": "bounded_clip",
                        "languages": list(_CATALOG_LOCALES),
                        "language_supported": None,
                        "ready": None,
                        "reason": "Readiness depends on the selected model.",
                        "language_reason": "",
                    },
                ],
                "audio_witness_models": [],
                "audio_witness_policy": "Audio witnesses do not supply timing.",
                "fallback": "mark_uncertain",
            },
        },
        "batch": {
            "id_namespace": "source_revision_cue",
            "source_revision_id": "revision-1",
            "cue_count": cue_count,
            "valid_cue_ids": list(range(1, cue_count + 1)),
            "cues": cues,
            "context": {
                "previous_output": [
                    {"text": "Earlier corrected wording", "speaker": "Speaker A"},
                    {"text": "Another earlier line", "speaker": "Speaker B"},
                ],
                "previous_source": [{"text": "Earlier source wording", "speaker": "Speaker A"}],
                "following_source": [{"text": "The next complete line", "speaker": "Speaker B"}],
            },
        },
        "delegation": {
            "execution_mode": "parallel",
            "max_parallel_batches": 3,
            "wave_number": 2,
            "wave_batch_count": 3,
            "context_capsule": {
                "overview": "Preserve the established terminology.",
                "terminology": {"term": "preferred term"},
                "entities": {"Speaker A": "narrator"},
                "style_rules": ["Keep punctuation."],
                "decisions": ["Use the glossary spelling."],
                "notes": ["New speakers remain proposals."],
            },
        },
    }


class _Application:
    def __init__(self):
        self.payload = _claim_payload()
        self.submit_call = None

    def claim_dispatch_batch(self, run_id, **kwargs):
        return copy.deepcopy(self.payload)

    def submit_dispatch_batch(self, batch_id, **kwargs):
        self.submit_call = {"batch_id": batch_id, **kwargs}
        return {"batch_id": batch_id, "run_id": "run-compact-1", "status": "accepted"}


def _claim(runtime, packet_format="standard", known_manifest_hash=None):
    return claim_dispatch_batch(
        runtime,
        ClaimDispatchBatchInput(
            run_id="run-compact-1",
            packet_format=packet_format,
            known_manifest_hash=known_manifest_hash,
            idempotency_key="claim:compact-test",
        ),
    ).result


class CompactDispatchTests(unittest.TestCase):
    def setUp(self):
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_default_claim_is_compact_and_explicit_standard_is_unchanged(self):
        default = claim_dispatch_batch(
            self.runtime,
            ClaimDispatchBatchInput(run_id="run-compact-1", idempotency_key="claim:default"),
        ).result
        self.assertEqual(_claim(self.runtime, "compact"), default)
        self.assertNotIn("packet_format", _claim(self.runtime, "standard"))

    def test_shared_manifest_hash_is_canonical_and_does_not_mutate_inputs(self):
        projected = {"lease_token": "lease", "batch": {"items": [1]}}
        manifest = {"z": "日本語", "a": {"revision": 1}}
        before = copy.deepcopy((projected, manifest))
        packet = compact_manifest_packet(projected, manifest, known_manifest_hash=None)
        reordered = compact_manifest_packet(
            projected, {"a": {"revision": 1}, "z": "日本語"}, known_manifest_hash=None
        )
        self.assertEqual(packet["manifest_hash"], reordered["manifest_hash"])
        self.assertEqual(before, (projected, manifest))
        self.assertIs(packet["batch"], projected["batch"])
        cached = compact_manifest_packet(
            projected, manifest, known_manifest_hash=packet["manifest_hash"]
        )
        self.assertEqual({k: v for k, v in packet.items() if k != "manifest"}, cached)

    def test_compact_claim_round_trips_cues_context_and_dynamic_task_data(self):
        standard = _claim(self.runtime)
        compact = _claim(self.runtime, "compact")

        self.assertEqual("compact-v1", compact["packet_format"])
        self.assertEqual(standard["run_id"], compact["run_id"])
        self.assertEqual(standard["batch_id"], compact["batch_id"])
        self.assertEqual(standard["lease_token"], compact["lease_token"])
        self.assertEqual(
            standard["batch"]["source_revision_id"], compact["batch"]["source_revision_id"]
        )
        self.assertEqual("a" * 64, compact["task"]["source_content_hash"])
        self.assertEqual(standard["task"]["known_speakers"], compact["task"]["known_speakers"])
        self.assertEqual(standard["task"]["glossary"], compact["task"]["glossary"])
        self.assertEqual(standard["batch"]["context"], compact["batch"]["context"])
        self.assertEqual(standard["delegation"], compact["delegation"])

        batch = compact["batch"]
        self.assertEqual(
            [
                "cue_id",
                "evidence_cue_ids",
                "text",
                "speaker",
                "turn_index",
                "start_ms",
                "end_ms",
                "gap_from_previous_ms",
                "overlap_with_previous_ms",
                "timing_basis",
            ],
            batch["cue_columns"],
        )
        self.assertEqual(standard["batch"]["cue_count"], len(batch["cue_rows"]))
        self.assertEqual(["turn-a", "turn-b"], batch["turns"])
        column_index = {name: index for index, name in enumerate(batch["cue_columns"])}
        standard_cues = standard["batch"]["cues"]
        for row, cue in zip(batch["cue_rows"], standard_cues, strict=True):
            self.assertEqual(cue["cue_id"], row[column_index["cue_id"]])
            self.assertEqual(cue["evidence_cue_ids"], row[column_index["evidence_cue_ids"]])
            self.assertEqual(cue["text"], row[column_index["text"]])
            self.assertEqual(cue["speaker"], row[column_index["speaker"]])
            self.assertEqual(cue["turn_id"], batch["turns"][row[column_index["turn_index"]]])
            for field in (
                "start_ms",
                "end_ms",
                "gap_from_previous_ms",
                "overlap_with_previous_ms",
            ):
                self.assertEqual(cue["timing"][field], row[column_index[field]])
            expected_basis = cue.get(
                "timing_basis",
                cue["timing"].get("timing_basis"),
            )
            self.assertEqual(expected_basis, row[column_index["timing_basis"]])

    def test_manifest_hash_cache_and_recovery(self):
        full = _claim(self.runtime, "compact")
        known = _claim(self.runtime, "compact", full["manifest_hash"])
        mismatch = _claim(self.runtime, "compact", "0" * 64)

        self.assertIn("manifest", full)
        self.assertNotIn("manifest", known)
        self.assertEqual(full["manifest_hash"], known["manifest_hash"])
        self.assertEqual(full["manifest"], mismatch["manifest"])
        self.assertEqual(full["manifest_hash"], mismatch["manifest_hash"])
        self.assertIn("instructions", full["manifest"])
        self.assertIn("quality_policy", full["manifest"])
        self.assertEqual("a" * 64, mismatch["task"]["source_content_hash"])

    def test_compact_manifest_prunes_route_language_arrays_before_hashing(self):
        raw = _claim_payload(cue_count=2)
        raw_before = copy.deepcopy(raw)
        projected = project_claim(raw)
        projected_before = copy.deepcopy(projected)
        compact = _compact_claim(projected, known_manifest_hash=None)
        cached = _compact_claim(
            projected,
            known_manifest_hash=compact["manifest_hash"],
        )
        standard = _claim(self.runtime, "standard")

        self.assertEqual(raw_before, raw)
        self.assertEqual(projected_before, projected)
        standard_policy = standard["task"]["quality_policy"]
        compact_policy = compact["manifest"]["quality_policy"]
        self.assertEqual(raw["task"]["quality_policy"], standard_policy)
        self.assertEqual("en", compact["manifest"]["source_language"])
        self.assertEqual(248, len(standard_policy["route_catalog"][0]["supported_languages"]))
        self.assertEqual(248, len(standard_policy["route_catalog"][1]["languages"]))
        for original_route, route in zip(
            standard_policy["route_catalog"],
            compact_policy["route_catalog"],
            strict=True,
        ):
            self.assertNotIn("supported_languages", route)
            self.assertNotIn("languages", route)
            self.assertEqual(
                {
                    key: value
                    for key, value in original_route.items()
                    if key not in {"supported_languages", "languages"}
                },
                route,
            )
        self.assertEqual(["whisper", "audio_llm"], [
            route["route"] for route in compact_policy["route_catalog"]
        ])
        whisper = compact_policy["route_catalog"][0]
        self.assertTrue(whisper["language_supported"])
        self.assertTrue(whisper["ready"])
        self.assertEqual("native_word", whisper["timing_method"])
        self.assertEqual(
            "preserve this unknown property",
            whisper["route_extension"],
        )
        self.assertEqual(
            {key: value for key, value in standard_policy.items() if key != "route_catalog"},
            {key: value for key, value in compact_policy.items() if key != "route_catalog"},
        )
        standard_policy_bytes = len(
            json.dumps(
                standard_policy,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        compact_policy_bytes = len(
            json.dumps(
                compact_policy,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        self.assertGreaterEqual(standard_policy_bytes - compact_policy_bytes, 3_000)
        self.assertEqual(compact["manifest_hash"], cached["manifest_hash"])
        self.assertEqual(compact["task"], cached["task"])
        self.assertNotIn("manifest", cached)

    def test_compact_packet_reduces_representative_claim_bytes(self):
        standard = _claim(self.runtime)
        compact = _claim(self.runtime, "compact")
        standard_bytes = len(
            json.dumps(standard, ensure_ascii=False, separators=(",", ":")).encode()
        )
        compact_bytes = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode())

        self.assertLess(compact_bytes, standard_bytes)

    def test_claim_and_submission_schemas_reject_ambiguous_or_invalid_compact_shapes(self):
        for invalid in (
            {"packet_format": "compact-v1"},
            {"known_manifest_hash": "not-a-hash"},
            {"unknown": True},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                ClaimDispatchBatchInput(
                    run_id="run-compact-1",
                    idempotency_key="claim:invalid-shape",
                    **invalid,
                )

        shared = {
            "batch_id": "batch-compact-1",
            "lease_token": "lease-capability",
            "idempotency_key": "submit:invalid-shape",
        }
        invalid_results = (
            {
                "kind": "correction",
                "operations": [],
                "edits": [{"cue_id": 1, "text": "Edited."}],
            },
            {
                "kind": "correction",
                "merges": [{"cue_ids": [1, 1], "texts": ["Merged."]}],
            },
            {"kind": "correction", "deletes": [1, 1]},
            {
                "kind": "correction",
                "merges": [{"cue_ids": [1], "texts": ["Not a merge."]}],
            },
            {
                "kind": "correction",
                "merges": [{"cue_ids": [1, 2], "texts": ["Merged."], "starts_new_turn": True}],
            },
            {
                "kind": "translation",
                "translations": [{"cue_id": 1, "text": "One."}],
                "items": [{"cue_id": 2, "text": "Two."}],
            },
            {"kind": "translation", "items": [{"cue_id": 1, "text": "One.", "extra": True}]},
        )
        for result in invalid_results:
            with self.subTest(result=result), self.assertRaises(ValidationError):
                SubmitDispatchBatchInput(**shared, result=result)

    def test_grouped_correction_and_translation_normalize_to_native_shapes(self):
        correction = SubmitDispatchBatchInput(
            batch_id="batch-compact-1",
            lease_token="lease-capability",
            result={
                "kind": "correction",
                "edits": [
                    {
                        "cue_id": 1,
                        "text": "Edited with its full text.",
                        "speaker": "Speaker A",
                        "starts_new_turn": True,
                    }
                ],
                "deletes": [2],
                "merges": [
                    {
                        "cue_ids": [3, 4],
                        "texts": ["One explicit merge."],
                    }
                ],
                "splits": [
                    {
                        "cue_id": 5,
                        "texts": ["First part.", "Second part."],
                        "split_boundary_ids": ["anchor:verified-1"],
                        "starts_new_turn": True,
                    }
                ],
                "uncertainties": [
                    {
                        "cue_id": 6,
                        "reason": "Unclear source word.",
                        "evidence_ids": ["evidence-6"],
                    }
                ],
            },
            idempotency_key="submit:compact-correction",
        )
        submit_dispatch_batch(self.runtime, correction)
        native_correction = self.application.submit_call["result"]
        self.assertEqual("correction", native_correction["kind"])
        operations = native_correction["operations"]
        self.assertEqual(
            ["edit", "delete", "merge", "split"],
            [operation["action"] for operation in operations],
        )
        self.assertEqual(
            [[1], [2], [3, 4], [5]], [operation["cue_ids"] for operation in operations]
        )
        self.assertEqual(
            [
                ["Edited with its full text."],
                [],
                ["One explicit merge."],
                ["First part.", "Second part."],
            ],
            [operation["texts"] for operation in operations],
        )
        self.assertEqual(
            [True, False, False, True], [operation["starts_new_turn"] for operation in operations]
        )
        self.assertEqual(["anchor:verified-1"], operations[3]["split_boundary_ids"])
        self.assertEqual(
            [{"cue_id": 6, "reason": "Unclear source word.", "evidence_ids": ["evidence-6"]}],
            native_correction["uncertainties"],
        )

        translation = SubmitDispatchBatchInput(
            batch_id="batch-compact-1",
            lease_token="lease-capability",
            result={
                "kind": "translation",
                "items": [{"cue_id": 7, "text": "Translated cue.", "speaker": "Speaker B"}],
                "glossary_updates": {"term": "preferred term"},
            },
            idempotency_key="submit:compact-translation",
        )
        submit_dispatch_batch(self.runtime, translation)
        self.assertEqual(
            {
                "kind": "translation",
                "translations": [
                    {
                        "cue_id": 7,
                        "cue_ids": None,
                        "text": "Translated cue.",
                        "speaker": "Speaker B",
                    }
                ],
                "glossary_updates": {"term": "preferred term"},
            },
            self.application.submit_call["result"],
        )


if __name__ == "__main__":
    unittest.main()
