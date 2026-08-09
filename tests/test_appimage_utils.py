import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import appimage_utils


class AppImageToolUtilsTests(unittest.TestCase):
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
