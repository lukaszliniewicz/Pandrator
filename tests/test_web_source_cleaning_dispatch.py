import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from sqlalchemy import select

from pandrator.logic.source_cleaning import SourceBlock, SourceDocument
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import (
    Artifact,
    SessionSource,
    SourceAsset,
    SourceCleaningDispatchBatch,
)
from pandrator.web.schemas import (
    SourceCleaningDispatchBatchClaimResponse,
    SourceCleaningDispatchBatchSubmitResponse,
)
from pandrator.web.source_cleaning_dispatch import _build_phase_packets
from tests.web_test_support import prepare_web_test_data_root


class SourceCleaningDispatchWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.client = self.app.test_client()
        token = bootstrap.issue()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.extension = self.app.extensions["pandrator"]

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def _headers(self, key: str | None = None):
        headers = {"X-CSRF-Token": self.csrf}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def _source(self):
        session = self.extension["sessions"].create(
            "Passive EPUB",
            workflow_kind="audiobook",
            source_language="en",
        )
        directory = self.extension["paths"].sessions / session.storage_key
        directory.mkdir(parents=True, exist_ok=True)
        source_path = directory / "book.epub"
        source_path.write_bytes(b"test-epub-placeholder")
        artifact = self.extension["artifacts"].register(
            source_path,
            kind="source",
            role="upload",
            session_id=session.id,
            metadata={"original_filename": "book.epub"},
        )
        source_asset = self.extension["source_library"].ensure_for_artifact(
            artifact.id,
            display_name="book.epub",
            kind="epub",
        )
        self.extension["source_library"].attach(session.id, source_asset.id)
        return session.id, artifact.id

    @staticmethod
    def _document(source_path: str) -> SourceDocument:
        return SourceDocument(
            source_type="epub_cleaned_text",
            source_path=source_path,
            filename="book.epub",
            language="en",
            metadata_candidates={
                "title": [{"value": "A Test Book", "source": "epub"}],
            },
            nav_titles=["Chapter One"],
            blocks=[
                SourceBlock(
                    block_id="block-0",
                    text="Table of Contents",
                    line_start=1,
                    line_end=1,
                    source_index=0,
                    href="toc.xhtml",
                    tag="nav",
                    role_candidates=["toc", "navigation"],
                ),
                SourceBlock(
                    block_id="block-1",
                    text="Chapter One",
                    line_start=2,
                    line_end=2,
                    source_index=1,
                    href="chapter-1.xhtml",
                    tag="h1",
                    role_candidates=["chapter_heading"],
                ),
                SourceBlock(
                    block_id="block-2",
                    text="Copyright 2026 Example Publisher",
                    line_start=3,
                    line_end=3,
                    source_index=2,
                    href="chapter-1.xhtml",
                    tag="p",
                    role_candidates=["copyright", "boilerplate"],
                ),
                SourceBlock(
                    block_id="block-3",
                    text="The narra tive begins here.",
                    line_start=4,
                    line_end=4,
                    source_index=3,
                    href="chapter-1.xhtml",
                    tag="p",
                ),
            ],
        )

    def _create(self, session_id: str, source_id: str):
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/source-cleaning-dispatch-runs",
            json={
                "source_artifact_id": source_id,
                "instructions": "Preserve the author's voice.",
                "evidence_limit": 2_000,
            },
            headers=self._headers("source-cleaning-create-1"),
        )
        self.assertEqual(202, response.status_code, response.get_json())
        return response.get_json()

    def test_epub_dispatch_prepares_without_provider_and_materializes_clean_text(self):
        session_id, source_id = self._source()
        created = self._create(session_id, source_id)
        self.assertEqual("preparing", created["status"])
        self.assertTrue(created["job_id"])

        premature = self.client.post(
            f"/api/v1/source-cleaning-dispatch-runs/{created['run_id']}/claim",
            json={},
            headers=self._headers("source-cleaning-premature-claim"),
        )
        self.assertEqual(409, premature.status_code)
        self.assertEqual("run_preparing", premature.get_json()["error"]["code"])

        document = self._document("book.epub")
        chapter_operation = {
            "op": "mark_chapter",
            "block_id": "block-1",
            "title": "Chapter One",
            "reason": "EPUB navigation heading",
            "confidence": 0.95,
        }
        with (
            patch(
                "pandrator.web.source_cleaning_dispatch.extract_clean_epub",
                return_value=document.plain_text(),
            ),
            patch(
                "pandrator.web.source_cleaning_dispatch.source_cleaning."
                "build_cleaned_epub_source_document",
                return_value=document,
            ),
            patch(
                "pandrator.web.source_cleaning_dispatch.source_cleaning."
                "propose_embedded_chapter_operations",
                return_value=[chapter_operation],
            ),
            patch(
                "pandrator.web.provider_settings.build_llm_settings",
                side_effect=AssertionError("Passive preparation called a provider."),
            ),
        ):
            prepared = self.extension["source_cleaning_dispatch"].prepare_run(
                created["run_id"], lambda _fraction, _message: None, Event()
            )
        self.assertEqual("ready", prepared["status"])
        self.assertEqual(6, prepared["batch_count"])

        final = None
        for ordinal in range(6):
            claim = self.client.post(
                f"/api/v1/source-cleaning-dispatch-runs/{created['run_id']}/claim",
                json={"lease_seconds": 900},
                headers=self._headers(f"source-cleaning-claim-{ordinal}"),
            )
            self.assertEqual(200, claim.status_code, claim.get_json())
            claimed = claim.get_json()
            SourceCleaningDispatchBatchClaimResponse.model_validate(claimed)
            phase = claimed["task"]["phase"]
            decisions = [
                {
                    "operation_id": proposal["operation_id"],
                    "verdict": "accept",
                }
                for proposal in claimed["batch"]["proposals"]
            ]
            operations = []
            if phase == "metadata":
                operations.append(
                    {
                        "op": "set_metadata",
                        "metadata": {"title": "A Test Book", "author": "Ada Author"},
                        "reason": "Confirmed from the source metadata.",
                    }
                )
            if phase == "navigation":
                self.assertIn("block-0", claimed["batch"]["valid_block_ids"])
                operations.append(
                    {
                        "op": "delete_blocks",
                        "block_ids": ["block-0"],
                        "reason": "Navigation label, not narration.",
                    }
                )
            if phase == "boilerplate":
                self.assertNotIn("block-0", claimed["batch"]["valid_block_ids"])
                self.assertIn("block-2", claimed["batch"]["valid_block_ids"])
                operations.append(
                    {
                        "op": "delete_blocks",
                        "block_ids": ["block-2"],
                        "reason": "Publisher boilerplate, not narration.",
                    }
                )
            if phase == "text_repair":
                self.assertIn("block-3", claimed["batch"]["valid_block_ids"])
                operations.append(
                    {
                        "op": "replace_block",
                        "block_id": "block-3",
                        "replacement": "The narrative begins here.",
                        "reason": "Repair an extraction split inside a word.",
                    }
                )
            submitted = self.client.post(
                f"/api/v1/source-cleaning-dispatch-batches/{claimed['batch_id']}/submit",
                json={
                    "lease_token": claimed["lease_token"],
                    "result": {
                        "kind": "source_cleaning",
                        "phase": phase,
                        "decisions": decisions,
                        "operations": operations,
                        "summary": f"Reviewed {phase}.",
                        "confidence": 0.9,
                    },
                },
                headers=self._headers(f"source-cleaning-submit-{ordinal}"),
            )
            self.assertEqual(200, submitted.status_code, submitted.get_json())
            final = submitted.get_json()
            SourceCleaningDispatchBatchSubmitResponse.model_validate(final)
            replayed_claim = self.client.post(
                f"/api/v1/source-cleaning-dispatch-runs/{created['run_id']}/claim",
                json={"lease_seconds": 900},
                headers=self._headers(f"source-cleaning-claim-{ordinal}"),
            )
            self.assertEqual(200, replayed_claim.status_code)
            self.assertEqual(
                claimed["lease_token"], replayed_claim.get_json()["lease_token"]
            )
            self.assertEqual("completed", replayed_claim.get_json()["batch_status"])

        assert final is not None
        self.assertEqual("completed", final["run_status"])
        self.assertTrue(final["finalized"])
        self.assertEqual(5, final["accepted_operation_count"])
        with self.extension["database"].session() as session:
            artifact = session.get(Artifact, final["result_artifact_id"])
            self.assertIsNotNone(artifact)
            assert artifact is not None
            output = Path(
                self.extension["paths"].managed_path(artifact.relative_path)
            ).read_text(encoding="utf-8")
        self.assertIn("[[Chapter]]Chapter One", output)
        self.assertIn("The narrative begins here.", output)
        self.assertNotIn("Table of Contents", output)
        self.assertNotIn("Copyright 2026", output)

    def test_leased_inspection_search_promotes_returned_blocks(self):
        session_id, source_id = self._source()
        created = self._create(session_id, source_id)
        document = self._document("book.epub")
        document.blocks.extend(
            SourceBlock(
                block_id=f"filler-{index}",
                text=f"Filler paragraph {index}.",
                line_start=10 + index,
                line_end=10 + index,
                source_index=10 + index,
                href="chapter-1.xhtml",
                tag="p",
            )
            for index in range(50)
        )
        document.blocks.append(
            SourceBlock(
                block_id="distant-defect",
                text="A uniquely bro ken extraction.",
                line_start=100,
                line_end=100,
                source_index=100,
                href="chapter-2.xhtml",
                tag="p",
            )
        )
        with (
            patch(
                "pandrator.web.source_cleaning_dispatch.extract_clean_epub",
                return_value=document.plain_text(),
            ),
            patch(
                "pandrator.web.source_cleaning_dispatch.source_cleaning."
                "build_cleaned_epub_source_document",
                return_value=document,
            ),
            patch(
                "pandrator.web.source_cleaning_dispatch.source_cleaning."
                "propose_embedded_chapter_operations",
                return_value=[],
            ),
        ):
            self.extension["source_cleaning_dispatch"].prepare_run(
                created["run_id"], lambda _fraction, _message: None, Event()
            )
        claim = self.client.post(
            f"/api/v1/source-cleaning-dispatch-runs/{created['run_id']}/claim",
            json={},
            headers=self._headers("source-cleaning-inspect-claim"),
        ).get_json()
        self.assertNotIn("distant-defect", claim["batch"]["valid_block_ids"])

        response = self.client.post(
            f"/api/v1/source-cleaning-dispatch-batches/{claim['batch_id']}/inspect",
            json={
                "lease_token": claim["lease_token"],
                "action": "search",
                "arguments": {"query": "uniquely bro ken", "max_hits": 10},
                "view": "working",
            },
            headers=self._headers("source-cleaning-inspect-search"),
        )
        self.assertEqual(200, response.status_code, response.get_json())
        inspected = response.get_json()
        self.assertEqual(["distant-defect"], inspected["promoted_block_ids"])
        self.assertEqual(
            "distant-defect", inspected["observation"][0]["block_id"]
        )
        replay = self.client.post(
            f"/api/v1/source-cleaning-dispatch-batches/{claim['batch_id']}/inspect",
            json={
                "lease_token": claim["lease_token"],
                "action": "search",
                "arguments": {"query": "uniquely bro ken", "max_hits": 10},
                "view": "working",
            },
            headers=self._headers("source-cleaning-inspect-search"),
        )
        self.assertEqual("true", replay.headers.get("Idempotency-Replayed"))
        with self.extension["database"].session() as db_session:
            batch = db_session.get(SourceCleaningDispatchBatch, claim["batch_id"])
            self.assertIsNotNone(batch)
            assert batch is not None
            self.assertIn("distant-defect", batch.input_json["valid_block_ids"])
            self.assertEqual(1, len(batch.input_json["inspection_log"]))

    def test_custom_operation_cannot_target_an_unexposed_block(self):
        session_id, source_id = self._source()
        created = self._create(session_id, source_id)
        document = self._document("book.epub")
        with (
            patch(
                "pandrator.web.source_cleaning_dispatch.extract_clean_epub",
                return_value=document.plain_text(),
            ),
            patch(
                "pandrator.web.source_cleaning_dispatch.source_cleaning."
                "build_cleaned_epub_source_document",
                return_value=document,
            ),
            patch(
                "pandrator.web.source_cleaning_dispatch.source_cleaning."
                "propose_embedded_chapter_operations",
                return_value=[],
            ),
        ):
            self.extension["source_cleaning_dispatch"].prepare_run(
                created["run_id"], lambda _fraction, _message: None, Event()
            )
        claim = self.client.post(
            f"/api/v1/source-cleaning-dispatch-runs/{created['run_id']}/claim",
            json={},
            headers=self._headers("source-cleaning-bounded-claim"),
        ).get_json()
        response = self.client.post(
            f"/api/v1/source-cleaning-dispatch-batches/{claim['batch_id']}/submit",
            json={
                "lease_token": claim["lease_token"],
                "result": {
                    "kind": "source_cleaning",
                    "phase": "metadata",
                    "decisions": [],
                    "operations": [
                        {
                            "op": "set_metadata",
                            "metadata": {"unsupported": "nope"},
                        }
                    ],
                },
            },
            headers=self._headers("source-cleaning-bounded-submit"),
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("invalid_model_response", response.get_json()["error"]["code"])

    def test_chapter_packet_exposes_heading_candidates_without_proposals(self):
        document = SourceDocument(
            source_type="epub_cleaned_text",
            source_path="book.epub",
            filename="book.epub",
            blocks=[
                SourceBlock(
                    block_id="title",
                    text="UNITARIAN CHRISTIANITY",
                    line_start=1,
                    line_end=1,
                    source_index=0,
                    href="text.xhtml",
                    tag="p",
                    role_candidates=["heading_candidate"],
                ),
                SourceBlock(
                    block_id="body",
                    text="The discourse begins here.",
                    line_start=2,
                    line_end=2,
                    source_index=1,
                    href="text.xhtml",
                    tag="p",
                ),
            ],
        )

        packets = _build_phase_packets(
            document,
            [],
            instructions="Preserve the author's voice.",
            evidence_limit=500,
        )
        chapter_packet = next(
            packet for packet in packets if packet["phase"] == "chapter_marking"
        )

        self.assertEqual([], chapter_packet["proposals"])
        self.assertIn("title", chapter_packet["valid_block_ids"])
        self.assertEqual(
            "title",
            chapter_packet["evidence"]["heading_candidates"][0]["block_id"],
        )
        self.assertEqual(
            "UNITARIAN CHRISTIANITY",
            chapter_packet["evidence"]["candidate_blocks"][0]["text"],
        )

    def test_pdf_chapter_packet_filters_advisory_deterministic_headings(self):
        document = SourceDocument(
            source_type="pdf_structured",
            source_path="book.pdf",
            filename="book.pdf",
            blocks=[
                SourceBlock(
                    block_id="decorative-title-fragment",
                    text="ARTHUR",
                    line_start=1,
                    line_end=1,
                    source_index=0,
                    page=1,
                    role_candidates=["heading", "deterministic_chapter"],
                ),
                SourceBlock(
                    block_id="chapter",
                    text="CHAPTER I",
                    line_start=2,
                    line_end=2,
                    source_index=1,
                    page=2,
                    role_candidates=["heading", "deterministic_chapter"],
                ),
            ],
        )
        deterministic_operations = [
            {
                "op": "mark_chapter",
                "block_id": block.block_id,
                "title": block.text,
                "reason": "PDF heading style",
            }
            for block in document.blocks
        ]

        packets = _build_phase_packets(
            document,
            deterministic_operations,
            instructions="Preserve the author's voice.",
            evidence_limit=500,
        )
        chapter_packet = next(
            packet for packet in packets if packet["phase"] == "chapter_marking"
        )

        self.assertEqual(
            ["chapter"],
            [item["operation"]["block_id"] for item in chapter_packet["proposals"]],
        )
        self.assertEqual(
            {"decorative-title-fragment", "chapter"},
            {
                item["block_id"]
                for item in chapter_packet["evidence"]["heading_candidates"]
            },
        )
        self.assertIn("decorative-title-fragment", chapter_packet["valid_block_ids"])

    def test_modern_source_must_still_be_attached(self):
        session_id, source_id = self._source()
        with self.extension["database"].session() as session:
            attachment = session.scalar(
                select(SessionSource)
                .join(SourceAsset, SourceAsset.id == SessionSource.source_asset_id)
                .where(
                    SessionSource.session_id == session_id,
                    SourceAsset.artifact_id == source_id,
                )
            )
            self.assertIsNotNone(attachment)
            assert attachment is not None
            attachment.is_current = False
            attachment.revision += 1
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/source-cleaning-dispatch-runs",
            json={"source_artifact_id": source_id},
            headers=self._headers("source-cleaning-detached-create"),
        )
        self.assertEqual(409, response.status_code)
        self.assertEqual(
            "source_session_mismatch", response.get_json()["error"]["code"]
        )
