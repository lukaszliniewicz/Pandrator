import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandrator.logic import xtts_trainer_handler


class XttsTrainerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.trainer = self.root / "trainer"
        self.models = self.root / "manager-models" / "xtts"

    def tearDown(self):
        self.temporary.cleanup()

    def _write_training_bundle(self, model_id="custom/narrator", *, files=None):
        source = self.trainer.joinpath(*model_id.split("/"), "models", "xtts-final")
        source.mkdir(parents=True)
        for filename, content in (files or {
            "config.json": b'{"model": "xtts"}',
            "model.pth": b"weights",
            "speakers_xtts.pth": b"speakers",
            "vocab.json": b"{}",
        }).items():
            (source / filename).write_bytes(content)
        return source

    def _paths(self):
        return {"trainer_dir": str(self.trainer), "xtts_models_dir": str(self.models)}

    def test_trained_bundle_promotes_atomically_into_manager_shared_wrapper_registry_root(self):
        self._write_training_bundle()
        with mock.patch.dict(
            os.environ,
            {"PANDRATOR_MODELS_DIR": str(self.root / "manager-models")},
            clear=False,
        ), mock.patch(
            "pandrator.logic.xtts_trainer_handler.os.replace", wraps=os.replace
        ) as promote:
            copied, _message = xtts_trainer_handler._copy_trained_model(
                "custom/narrator", self._paths()
            )

        self.assertTrue(copied)
        published = self.models / "custom" / "narrator"
        self.assertTrue((published / "config.json").is_file())
        self.assertTrue((published / "model.pth").is_file())
        staging, target = (Path(value) for value in promote.call_args.args)
        self.assertEqual(self.models / ".downloads", staging.parent)
        self.assertEqual(published, target)
        self.assertFalse(any((self.models / ".downloads").iterdir()))

    def test_promotion_copies_only_exact_wrapper_bundle(self):
        source = self._write_training_bundle()
        (source / "run").mkdir()
        (source / "run" / "checkpoint.pth").write_bytes(b"checkpoint")
        (source / "extra.bin").write_bytes(b"extra")

        copied, _message = xtts_trainer_handler._copy_trained_model(
            "custom/narrator", self._paths()
        )

        self.assertTrue(copied)
        published = self.models / "custom" / "narrator"
        self.assertEqual(
            set(xtts_trainer_handler.XTTS_MODEL_BUNDLE_FILENAMES),
            {path.name for path in published.iterdir()},
        )

    def test_existing_bundle_is_preserved_without_overwrite(self):
        self._write_training_bundle()
        existing = self.models / "custom" / "narrator"
        existing.mkdir(parents=True)
        (existing / "sentinel.txt").write_text("keep", encoding="utf-8")

        copied, message = xtts_trainer_handler._copy_trained_model(
            "custom/narrator", self._paths()
        )

        self.assertFalse(copied)
        self.assertIn("never overwritten", message)
        self.assertEqual("keep", (existing / "sentinel.txt").read_text(encoding="utf-8"))

    def test_rejects_incomplete_bundle_and_traversal_model_id(self):
        self._write_training_bundle(
            files={"config.json": b"{}", "model.pth": b"weights"}
        )
        copied, message = xtts_trainer_handler._copy_trained_model(
            "custom/narrator", self._paths()
        )
        self.assertFalse(copied)
        self.assertIn("incomplete", message)
        self.assertFalse((self.models / "custom" / "narrator").exists())

        copied, message = xtts_trainer_handler._copy_trained_model(
            "../escape", self._paths()
        )
        self.assertFalse(copied)
        self.assertIn("unsafe", message)

    def test_rejects_wrapper_ignored_model_path_parts(self):
        for model_id in ("__pycache__", "team/__pycache__/voice"):
            with self.subTest(model_id=model_id):
                copied, message = xtts_trainer_handler._copy_trained_model(
                    model_id, self._paths()
                )

                self.assertFalse(copied)
                self.assertIn("unsafe", message)
                self.assertFalse(self.models.joinpath(*model_id.split("/")).exists())


if __name__ == "__main__":
    unittest.main()
