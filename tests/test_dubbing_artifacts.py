import os
import tempfile
import unittest
from pathlib import Path

from pandrator.logic.dubbing import artifacts


class DubbingArtifactResolverTests(unittest.TestCase):
    def _write_file(self, directory: str, name: str, mtime: float = 1000.0) -> str:
        path = os.path.join(directory, name)
        Path(path).write_text("data", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_resolve_active_artifact_filters_missing_and_suffix_mismatch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            active_srt = self._write_file(temp_dir, "active.srt")

            def get_active(_session_name, _roles):
                return active_srt

            self.assertEqual(
                artifacts.resolve_active_artifact_path(
                    "Session",
                    ["translated_srt"],
                    get_active,
                    suffixes=(".srt",),
                ),
                active_srt,
            )
            self.assertEqual(
                artifacts.resolve_active_artifact_path(
                    "Session",
                    ["translated_srt"],
                    get_active,
                    suffixes=(".json",),
                ),
                "",
            )

            missing_path = os.path.join(temp_dir, "missing.srt")

            def get_missing(_session_name, _roles):
                return missing_path

            self.assertEqual(
                artifacts.resolve_active_artifact_path("Session", ["translated_srt"], get_missing),
                "",
            )

    def test_find_latest_srt_prefers_active_artifact(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = os.path.join(temp_dir, "work")
            root_dir = os.path.join(temp_dir, "root")
            os.makedirs(work_dir)
            os.makedirs(root_dir)
            active_srt = self._write_file(work_dir, "active.srt", 1000.0)
            self._write_file(work_dir, "newer.srt", 2000.0)

            result = artifacts.find_latest_srt(
                "Session",
                work_dir,
                lambda _session_name: root_dir,
                lambda _session_name, _roles: active_srt,
            )

            self.assertEqual(result, active_srt)

    def test_find_latest_srt_excludes_equalized_when_requested(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = os.path.join(temp_dir, "work")
            root_dir = os.path.join(temp_dir, "root")
            os.makedirs(work_dir)
            os.makedirs(root_dir)
            equalized_srt = self._write_file(work_dir, "clip_equalized.srt", 2000.0)
            base_srt = self._write_file(work_dir, "clip.srt", 1000.0)

            result = artifacts.find_latest_srt(
                "Session",
                work_dir,
                lambda _session_name: root_dir,
                lambda _session_name, _roles: equalized_srt,
                must_not_be_equalized=True,
            )

            self.assertEqual(result, base_srt)

    def test_find_latest_srt_uses_work_dir_on_mtime_tie(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = os.path.join(temp_dir, "work")
            root_dir = os.path.join(temp_dir, "root")
            os.makedirs(work_dir)
            os.makedirs(root_dir)
            work_srt = self._write_file(work_dir, "work.srt", 1000.0)
            self._write_file(root_dir, "root.srt", 1000.0)

            result = artifacts.find_latest_srt(
                "Session",
                work_dir,
                lambda _session_name: root_dir,
                lambda _session_name, _roles: "",
            )

            self.assertEqual(result, work_srt)

    def test_discover_latest_file_with_suffix_prefers_active_artifact_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            active_blocks = self._write_file(temp_dir, "active_speech_blocks.json", 1000.0)
            self._write_file(temp_dir, "newer_speech_blocks.json", 2000.0)

            result = artifacts.discover_latest_file_with_suffix(
                "Session",
                temp_dir,
                "_speech_blocks.json",
                lambda _session_name, roles: active_blocks if roles == ["speech_blocks"] else "",
            )

            self.assertEqual(result, active_blocks)


if __name__ == "__main__":
    unittest.main()
