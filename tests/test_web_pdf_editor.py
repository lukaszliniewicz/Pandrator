import json
import tempfile
import unittest
from pathlib import Path

import fitz

from pandrator.web.pdf_editor import PdfEditPlan, apply_pdf_edit_plan, inspect_pdf, page_side


class PdfEditorTests(unittest.TestCase):
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
            self.assertEqual(destination, output)
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


if __name__ == "__main__":
    unittest.main()

