import hashlib
import json
import tempfile
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pymupdf as fitz
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.web import pdf_editor
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import Artifact, ArtifactEdge, Job
from pandrator.web.pdf_editor import (
    PdfEditPlan,
    PdfRect,
    apply_pdf_edit_plan,
    inspect_pdf,
    page_side,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class PdfEditorTests(unittest.TestCase):
    def test_rectangles_reject_unrepresentable_numbers_as_validation_errors(self):
        for key in ("x0", "y0", "x1", "y1"):
            for value in (10**400, -(10**400)):
                with self.subTest(key=key, sign=value > 0):
                    coordinates = {"x0": 0, "y0": 0, "x1": 10, "y1": 10}
                    coordinates[key] = value
                    with self.assertRaisesRegex(ValueError, "numeric"):
                        PdfRect.from_value(coordinates)

    def test_whiteout_colors_require_finite_numeric_values_and_keep_clamping(self):
        whiteout = {"original_page": 0, "rect": {"x0": 0, "y0": 0, "x1": 10, "y1": 10}}
        for value in (None, "bad", 10**400, -(10**400), float("nan"), float("inf"), -float("inf")):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "Whiteout color"):
                    PdfEditPlan.from_value({"whiteouts": [{**whiteout, "color": [value, 0, 0]}]})
        plan = PdfEditPlan.from_value({"whiteouts": [{**whiteout, "color": ["0.25", 2, -1]}]})
        self.assertEqual((0.25, 1.0, 0.0), plan.whiteouts[0].color)

    def test_whiteout_removes_text_and_retains_embedded_image_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "image-source.pdf"
            destination = Path(directory) / "whiteout.pdf"
            with fitz.open() as document:
                page = document.new_page(width=200, height=200)
                image = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 10, 10), False)
                image.clear_with(128)
                page.insert_image(fitz.Rect(20, 20, 100, 100), pixmap=image)
                page.insert_text((30, 60), "Remove me")
                document.save(source)
                image_bytes = document.extract_image(page.get_images()[0][0])["image"]
            original = source.read_bytes()
            plan = PdfEditPlan.from_value(
                {
                    "whiteouts": [
                        {
                            "original_page": 0,
                            "rect": {"x0": 20, "y0": 20, "x1": 100, "y1": 100},
                        }
                    ]
                }
            )
            apply_pdf_edit_plan(source, destination, plan)
            with fitz.open(destination) as document:
                self.assertNotIn("Remove me", document[0].get_text("text"))
                images = document[0].get_images()
                self.assertEqual(1, len(images))
                self.assertEqual(image_bytes, document.extract_image(images[0][0])["image"])
            self.assertEqual(original, source.read_bytes())

    def create_pdf(self, path: Path):
        document = fitz.open()
        sizes = [(300, 500), (320, 500), (300, 520), (340, 540)]
        for index, (width, height) in enumerate(sizes):
            page = document.new_page(width=width, height=height)
            page.insert_text((40, 60), f"Original page {index + 1}")
            if index == 1:
                page.set_rotation(90)
        document.save(path)
        document.close()

    def test_left_right_membership_is_stable_by_original_page(self):
        self.assertEqual([page_side(index, "right") for index in range(4)], ["right", "left", "right", "left"])
        self.assertEqual([page_side(index, "left") for index in range(4)], ["left", "right", "left", "right"])

    def test_inspection_preserves_mixed_geometry_and_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mixed.pdf"
            self.create_pdf(source)
            metadata = inspect_pdf(source, first_page_side="left")
            self.assertEqual(metadata["page_count"], 4)
            self.assertEqual(metadata["pages"][0]["side"], "left")
            self.assertEqual(metadata["pages"][1]["side"], "right")
            self.assertEqual(metadata["pages"][1]["rotation"], 90)
            self.assertEqual(metadata["pages"][3]["width"], 340)

    def test_apply_creates_derived_pdf_and_versioned_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            output = root / "edited.pdf"
            self.create_pdf(source)
            plan = PdfEditPlan.from_value(
                {
                    "first_page_side": "right",
                    "crops": [
                        {"original_page": 0, "rect": {"x0": 20, "y0": 20, "x1": 280, "y1": 470}}
                    ],
                    "whiteouts": [
                        {
                            "original_page": 1,
                            "rect": {"x0": 30, "y0": 30, "x1": 150, "y1": 80},
                            "color": [1, 1, 1],
                        }
                    ],
                    "deleted_pages": [2],
                }
            )
            destination, manifest, provenance = apply_pdf_edit_plan(
                source, output, plan, parent_artifact_id="parent-id"
            )
            self.assertEqual(destination, output.resolve())
            self.assertTrue(source.is_file())
            self.assertTrue(output.is_file())
            self.assertTrue(manifest.is_file())

            edited = fitz.open(output)
            self.assertEqual(edited.page_count, 3)
            self.assertAlmostEqual(edited[0].cropbox.width, 260, places=2)
            edited.close()

            stored = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(stored["schema"], "pandrator.pdf-edit")
            self.assertEqual(stored["version"], 1)
            self.assertEqual(stored["parent_artifact_id"], "parent-id")
            self.assertEqual([item["original_page"] for item in stored["page_map"]], [0, 1, 3])
            self.assertEqual([item["side"] for item in stored["page_map"]], ["right", "left", "left"])
            self.assertEqual(stored["operation_order"], ["whiteout", "crop", "delete"])
            self.assertEqual(provenance["output"]["page_count"], 3)

    def test_rejects_source_overwrite_and_out_of_bounds_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            self.create_pdf(source)
            with self.assertRaisesRegex(ValueError, "source overwrite"):
                apply_pdf_edit_plan(source, source, PdfEditPlan.from_value({}))
            bad_plan = PdfEditPlan.from_value(
                {
                    "crops": [
                        {"original_page": 0, "rect": {"x0": -10, "y0": 0, "x1": 300, "y1": 500}}
                    ]
                }
            )
            with self.assertRaisesRegex(ValueError, "escapes the MediaBox"):
                apply_pdf_edit_plan(source, Path(directory) / "bad.pdf", bad_plan)


class PdfWorkerPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pdf-worker-publication-")
        self.addCleanup(temporary.cleanup)
        self.paths = prepare_web_test_data_root(temporary.name)
        self.databases = [Database(self.paths.database) for _ in range(3)]
        for database in self.databases:
            self.addCleanup(database.dispose)
        self.database = self.databases[0]
        self.queue = JobQueue(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.session_record = SessionService(self.database).create("PDF publication fixture")
        self.session_dir = self.paths.sessions / self.session_record.storage_key
        self.handlers = [WorkflowHandlers(database, self.paths) for database in self.databases[1:]]
        for handlers in self.handlers:
            self.addCleanup(handlers.tts_providers.close)
        self.workers = [
            Worker(JobQueue(database), f"pdf-worker-{index}", handlers.handler_registry)
            for index, (database, handlers) in enumerate(
                zip(self.databases[1:], self.handlers, strict=True), start=1
            )
        ]
        self.source_path = self.paths.uploads / "source.pdf"
        document = fitz.open()
        try:
            for index in range(3):
                page = document.new_page()
                page.insert_text((72, 72), f"page{index}")
            document.save(self.source_path)
        finally:
            document.close()
        self.source_bytes = self.source_path.read_bytes()
        self.source_hash = self._sha(self.source_path)
        self.source_artifact = self.artifacts.register(
            self.source_path, kind="source", role="upload", session_id=self.session_record.id
        )
        self.initial_files = self._managed_files()
        self.helper_destinations: list[Path] = []

    def _sha(self, path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _enqueue(self, deleted_pages: list[int], *, queue_session: bool = True) -> Job:
        return self.queue.enqueue(
            "pdf.apply_edits",
            {
                "session_id": self.session_record.id,
                "source_artifact_id": self.source_artifact.id,
                "plan": {"deleted_pages": deleted_pages},
            },
            session_id=self.session_record.id if queue_session else None,
        )

    def _artifact_records(self) -> dict[str, dict[str, Any]]:
        with self.database.session() as session:
            artifacts = list(session.scalars(select(Artifact)))
            edges = list(session.scalars(select(ArtifactEdge)))
            return {
                artifact.id: {
                    "row": {
                        column.name: getattr(artifact, column.name)
                        for column in Artifact.__table__.columns
                    },
                    "parents": sorted(
                        (edge.parent_artifact_id, edge.relation)
                        for edge in edges if edge.child_artifact_id == artifact.id
                    ),
                }
                for artifact in artifacts
            }

    def _derived_records(self) -> dict[str, dict[str, Any]]:
        return {
            artifact_id: record
            for artifact_id, record in self._artifact_records().items()
            if artifact_id != self.source_artifact.id
        }

    def _managed_files(self) -> set[Path]:
        return {
            path for path in self.paths.root.rglob("*")
            if path.is_file() and not str(path).startswith(str(self.paths.database))
        }

    def _page_text(self, page: fitz.Page) -> str:
        text = page.get_text("text")
        self.assertIsInstance(text, str)
        assert isinstance(text, str)
        return text.strip()

    def _assert_source_intact(self) -> None:
        self.assertEqual(self.source_bytes, self.source_path.read_bytes())
        self.assertEqual(self.source_hash, self._sha(self.source_path))
        with fitz.open(self.source_path) as document:
            self.assertEqual(["page0", "page1", "page2"], [self._page_text(page) for page in document])

    def _assert_files_and_attempts_clean(self, expected_outputs: set[Path]) -> None:
        self.assertEqual(self.initial_files | expected_outputs, self._managed_files())
        for destination in self.helper_destinations:
            if destination.parent != self.session_dir and destination.parent not in {
                path.parent for path in self.initial_files
            }:
                self.assertFalse(destination.parent.exists(), str(destination.parent))

    def _output_snapshot(self, job_id: str, expected_pages: list[str]) -> dict[str, Any]:
        job = self.queue.get(job_id)
        self.assertEqual("succeeded", job.status, job.error_message)
        result = job.result_json
        self.assertIsInstance(result, dict)
        assert isinstance(result, dict)
        self.assertEqual(len(expected_pages), result["page_count"])
        pdf_artifact, pdf_path = self.artifacts.resolve(result["artifact_id"])
        manifest_artifact, manifest_path = self.artifacts.resolve(result["manifest_artifact_id"])
        self.assertEqual("pdf_edited", pdf_artifact.role)
        self.assertEqual("provenance", manifest_artifact.role)
        self.assertEqual(self.session_record.id, pdf_artifact.session_id)
        self.assertEqual(self.session_record.id, manifest_artifact.session_id)
        self.assertEqual("current", pdf_artifact.state)
        self.assertEqual("current", manifest_artifact.state)
        self.assertEqual(pdf_artifact.content_hash, self._sha(pdf_path))
        self.assertEqual(manifest_artifact.content_hash, self._sha(manifest_path))
        manifest_bytes = manifest_path.read_bytes()
        provenance = json.loads(manifest_bytes)
        self.assertEqual("pandrator.pdf-edit", provenance["schema"])
        self.assertEqual(1, provenance["version"])
        self.assertEqual(str(self.source_path.resolve()), provenance["source"]["path"])
        self.assertEqual(self.source_hash, provenance["source"]["sha256"])
        self.assertEqual(3, provenance["source"]["page_count"])
        self.assertEqual(str(pdf_path.resolve()), provenance["output"]["path"])
        self.assertEqual(self._sha(pdf_path), provenance["output"]["sha256"])
        self.assertEqual(len(expected_pages), provenance["output"]["page_count"])
        self.assertEqual(self.source_artifact.id, provenance["parent_artifact_id"])
        self.assertEqual(
            self.paths.relative_managed_path(manifest_path),
            pdf_artifact.metadata_json["provenance_manifest"],
        )
        with fitz.open(pdf_path) as document:
            self.assertEqual(len(expected_pages), document.page_count)
            self.assertEqual(expected_pages, [self._page_text(page) for page in document])
        records = self._artifact_records()
        self.assertEqual([(self.source_artifact.id, "derived_from")], records[pdf_artifact.id]["parents"])
        self.assertEqual(
            sorted([(self.source_artifact.id, "derived_from"), (pdf_artifact.id, "derived_from")]),
            records[manifest_artifact.id]["parents"],
        )
        self._assert_source_intact()
        return {
            "result": result,
            "pdf_path": pdf_path,
            "manifest_path": manifest_path,
            "pdf_bytes": pdf_path.read_bytes(),
            "manifest_bytes": manifest_bytes,
            "pdf_record": records[pdf_artifact.id],
            "manifest_record": records[manifest_artifact.id],
        }

    def _output_paths(self, snapshot: dict[str, Any]) -> set[Path]:
        return {snapshot["pdf_path"], snapshot["manifest_path"]}

    def _finish_paused_worker(
        self, release: threading.Event, thread: threading.Thread, outcome: dict[str, Any]
    ) -> None:
        release.set()
        thread.join(10)
        self.assertFalse(thread.is_alive(), "Paused PDF worker failed to join within 10 seconds.")
        self.assertNotIn("exception", outcome)
        self.assertTrue(outcome["returned"])

    @contextmanager
    def _observed_pdf_helper(self) -> Iterator[None]:
        real_apply = pdf_editor.apply_pdf_edit_plan

        def observed_apply(
            source: Path, destination: Path, plan: PdfEditPlan, *, parent_artifact_id: str | None = None
        ) -> tuple[Path, Path, dict[str, Any]]:
            self.helper_destinations.append(destination)
            return real_apply(source, destination, plan, parent_artifact_id=parent_artifact_id)

        with patch.object(pdf_editor, "apply_pdf_edit_plan", observed_apply):
            yield

    @contextmanager
    def _paused_first_worker(
        self,
    ) -> Iterator[tuple[threading.Event, threading.Thread, dict[str, Any]]]:
        entered = threading.Event()
        release = threading.Event()
        outcome: dict[str, Any] = {}
        call_lock = threading.Lock()
        invocation = 0
        real_apply = pdf_editor.apply_pdf_edit_plan

        def scheduled_apply(
            source: Path, destination: Path, plan: PdfEditPlan, *, parent_artifact_id: str | None = None
        ) -> tuple[Path, Path, dict[str, Any]]:
            nonlocal invocation
            with call_lock:
                invocation += 1
                first = invocation == 1
                self.helper_destinations.append(destination)
            if first:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("First PDF helper was not released within 10 seconds.")
            return real_apply(source, destination, plan, parent_artifact_id=parent_artifact_id)

        def run_first() -> None:
            try:
                outcome["returned"] = self.workers[0].run_once()
            except Exception as error:
                outcome["exception"] = error

        with patch.object(pdf_editor, "apply_pdf_edit_plan", scheduled_apply):
            thread = threading.Thread(target=run_first, name="pdf-first-paused")
            thread.start()
            try:
                self.assertTrue(entered.wait(10), "First worker did not reach the real PDF helper.")
                yield release, thread, outcome
            finally:
                release.set()
                thread.join(10)
                self.assertFalse(thread.is_alive(), "PDF worker remained alive during cleanup.")
                self.assertNotIn("exception", outcome)

    def test_ordinary_session_jobs_publish_distinct_consistent_outputs(self) -> None:
        first = self._enqueue([1])
        second = self._enqueue([])
        with self._paused_first_worker() as (release, thread, outcome):
            self.assertFalse(self.workers[1].run_once())
            self.assertEqual("queued", self.queue.get(second.id).status)
            self._finish_paused_worker(release, thread, outcome)
            self.assertTrue(self.workers[1].run_once())
        first_output = self._output_snapshot(first.id, ["page0", "page2"])
        second_output = self._output_snapshot(second.id, ["page0", "page1", "page2"])
        self.assertNotEqual(first_output["result"]["artifact_id"], second_output["result"]["artifact_id"])
        self.assertNotEqual(first_output["result"]["manifest_artifact_id"], second_output["result"]["manifest_artifact_id"])
        self.assertEqual("source_edited.pdf", first_output["pdf_path"].name)
        self.assertEqual("source_edited_2.pdf", second_output["pdf_path"].name)
        self.assertEqual(4, len(self._derived_records()))
        self._assert_files_and_attempts_clean(self._output_paths(first_output) | self._output_paths(second_output))

    def test_canceled_first_cannot_change_second_completed_publication(self) -> None:
        first = self._enqueue([1])
        second = self._enqueue([])
        with self._paused_first_worker() as (release, thread, outcome):
            canceled = self.queue.request_cancel(first.id)
            self.assertEqual("cancel_requested", canceled.status)
            self.assertIsNotNone(canceled.lease_owner)
            self.assertTrue(self.workers[1].run_once())
            before = self._output_snapshot(second.id, ["page0", "page1", "page2"])
            before_records = self._derived_records()
            self._finish_paused_worker(release, thread, outcome)
        self.assertEqual("canceled", self.queue.get(first.id).status)
        self.assertEqual(before, self._output_snapshot(second.id, ["page0", "page1", "page2"]))
        self.assertEqual(before_records, self._derived_records())
        self.assertEqual(2, len(self._derived_records()))
        self._assert_files_and_attempts_clean(self._output_paths(before))

    def test_null_queue_session_jobs_publish_independent_stable_outputs(self) -> None:
        first = self._enqueue([1], queue_session=False)
        second = self._enqueue([], queue_session=False)
        self.assertIsNone(first.session_id)
        self.assertIsNone(second.session_id)
        with self._paused_first_worker() as (release, thread, outcome):
            self.assertTrue(self.workers[1].run_once())
            before = self._output_snapshot(second.id, ["page0", "page1", "page2"])
            self._finish_paused_worker(release, thread, outcome)
        first_output = self._output_snapshot(first.id, ["page0", "page2"])
        self.assertEqual(before, self._output_snapshot(second.id, ["page0", "page1", "page2"]))
        self.assertNotEqual(first_output["result"]["artifact_id"], before["result"]["artifact_id"])
        self.assertNotEqual(first_output["result"]["manifest_artifact_id"], before["result"]["manifest_artifact_id"])
        self.assertNotEqual(first_output["pdf_path"], before["pdf_path"])
        self.assertNotEqual(first_output["manifest_path"], before["manifest_path"])
        self.assertEqual(4, len(self._derived_records()))
        self._assert_files_and_attempts_clean(self._output_paths(first_output) | self._output_paths(before))

    def test_missing_historical_files_do_not_reuse_registered_paths_or_rows(self) -> None:
        first = self._enqueue([1])
        with self._observed_pdf_helper():
            self.assertTrue(self.workers[0].run_once())
        original = self._output_snapshot(first.id, ["page0", "page2"])
        original_records = self._derived_records()
        for path in self._output_paths(original):
            path.unlink()
        second = self._enqueue([])
        with self._observed_pdf_helper():
            self.assertTrue(self.workers[1].run_once())
        new = self._output_snapshot(second.id, ["page0", "page1", "page2"])
        records = self._derived_records()
        self.assertEqual(original["result"], self.queue.get(first.id).result_json)
        self.assertNotEqual(original["result"]["artifact_id"], new["result"]["artifact_id"])
        self.assertNotEqual(original["result"]["manifest_artifact_id"], new["result"]["manifest_artifact_id"])
        self.assertEqual("source_edited.pdf", original["pdf_path"].name)
        self.assertEqual("source_edited_2.pdf", new["pdf_path"].name)
        self.assertNotEqual(original["manifest_path"], new["manifest_path"])
        for artifact_id, record in original_records.items():
            self.assertEqual(record, records[artifact_id])
        for path in self._output_paths(original):
            self.assertFalse(path.exists())
        self.assertEqual(4, len(records))
        self._assert_files_and_attempts_clean(self._output_paths(new))

    def test_orphan_manifest_is_preserved_and_new_pair_is_consistent(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        orphan = self.session_dir / "source_edited.pdf.pandrator.json"
        sentinel = b"unregistered orphan manifest sentinel"
        orphan.write_bytes(sentinel)
        self.assertFalse((self.session_dir / "source_edited.pdf").exists())
        job = self._enqueue([])
        with self._observed_pdf_helper():
            self.assertTrue(self.workers[0].run_once())
        output = self._output_snapshot(job.id, ["page0", "page1", "page2"])
        self.assertEqual(sentinel, orphan.read_bytes())
        self.assertEqual("source_edited_2.pdf", output["pdf_path"].name)
        self.assertNotEqual(orphan, output["manifest_path"])
        self.assertEqual(2, len(self._derived_records()))
        self._assert_files_and_attempts_clean(self._output_paths(output) | {orphan})

    def test_stale_worker_generation_cannot_publish_outputs(self) -> None:
        job = self._enqueue([1])
        with self._paused_first_worker() as (release, thread, outcome):
            with self.database.immediate_session() as session:
                current = session.get(Job, job.id)
                assert current is not None
                previous_generation = current.lease_generation
                current.lease_generation += 1
            self._finish_paused_worker(release, thread, outcome)
        current = self.queue.get(job.id)
        self.assertEqual(previous_generation + 1, current.lease_generation)
        self.assertEqual({}, self._derived_records())
        self._assert_source_intact()
        self._assert_files_and_attempts_clean(set())

    def test_manifest_registration_failure_rolls_back_and_cleans_pair(self) -> None:
        job = self._enqueue([1])
        real_registration = ArtifactService.register_in_session
        roles: list[str] = []

        def fail_manifest_registration(
            service: ArtifactService, session: Session, path: Path, **kwargs: Any
        ) -> Artifact:
            role = str(kwargs.get("role") or "artifact")
            roles.append(role)
            if role == "provenance":
                raise RuntimeError("fixed PDF manifest registration failure")
            return real_registration(service, session, path, **kwargs)

        with (
            self._observed_pdf_helper(),
            patch.object(ArtifactService, "register_in_session", fail_manifest_registration),
        ):
            self.assertTrue(self.workers[0].run_once())
        current = self.queue.get(job.id)
        self.assertEqual("failed", current.status)
        self.assertEqual("RuntimeError", current.error_code)
        self.assertIn("fixed PDF manifest registration failure", current.error_message or "")
        self.assertIn("pdf_edited", roles)
        self.assertIn("provenance", roles)
        self.assertEqual({}, self._derived_records())
        with self.database.session() as session:
            self.assertEqual([], list(session.scalars(select(ArtifactEdge))))
        self._assert_source_intact()
        self._assert_files_and_attempts_clean(set())


if __name__ == "__main__":
    unittest.main()
