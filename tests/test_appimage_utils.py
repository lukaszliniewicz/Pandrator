import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import appimage_utils


class AppImageToolUtilsTests(unittest.TestCase):
    def test_versioned_download_preserves_legacy_cache_and_reuses_verified_tool(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            legacy_target = cache_dir / "appimagetool-x86_64.AppImage"
            legacy_bytes = b"legacy cached appimagetool"
            legacy_target.write_bytes(legacy_bytes)

            downloaded_bytes = b"accepted pinned appimagetool bytes"
            target = (
                cache_dir
                / f"appimagetool-{appimage_utils.APPIMAGETOOL_VERSION}-x86_64.AppImage"
            )
            expected_url = (
                "https://github.com/AppImage/appimagetool/releases/download/"
                f"{appimage_utils.APPIMAGETOOL_VERSION}/appimagetool-x86_64.AppImage"
            )

            def retrieve(url: str, destination: str):
                self.assertEqual(expected_url, url)
                Path(destination).write_bytes(downloaded_bytes)
                return destination, None

            expected_digest = hashlib.sha256(downloaded_bytes).hexdigest()
            with patch.dict(
                appimage_utils.APPIMAGETOOL_SHA256,
                {"x86_64": expected_digest},
            ), patch.object(
                appimage_utils.urllib.request, "urlretrieve", side_effect=retrieve
            ) as mocked_retrieve:
                selected = appimage_utils.download_appimagetool(cache_dir, "x86_64")
                self.assertEqual(target, selected)
                self.assertEqual(downloaded_bytes, selected.read_bytes())
                self.assertEqual(legacy_bytes, legacy_target.read_bytes())
                mocked_retrieve.assert_called_once()

                reused = appimage_utils.download_appimagetool(cache_dir, "x86_64")
                self.assertEqual(target, reused)
                self.assertEqual(downloaded_bytes, reused.read_bytes())
                self.assertEqual(legacy_bytes, legacy_target.read_bytes())
                mocked_retrieve.assert_called_once()

    def test_appimagetool_checksum_rejects_changed_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            tool_path = Path(directory) / "appimagetool.AppImage"
            tool_path.write_bytes(b"expected appimagetool")
            expected = appimage_utils.sha256_file(tool_path)

            with patch.dict(
                appimage_utils.APPIMAGETOOL_SHA256,
                {"x86_64": expected},
            ):
                appimage_utils.verify_appimagetool(tool_path, "x86_64")
                tool_path.write_bytes(b"changed appimagetool")
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    appimage_utils.verify_appimagetool(tool_path, "x86_64")


if __name__ == "__main__":
    unittest.main()
