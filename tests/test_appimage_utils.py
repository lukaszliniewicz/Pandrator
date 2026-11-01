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
                cache_dir / f"appimagetool-{appimage_utils.APPIMAGETOOL_VERSION}-x86_64.AppImage"
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
            with (
                patch.dict(
                    appimage_utils.APPIMAGETOOL_SHA256,
                    {"x86_64": expected_digest},
                ),
                patch.object(
                    appimage_utils.urllib.request, "urlretrieve", side_effect=retrieve
                ) as mocked_retrieve,
            ):
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

    def test_versioned_runtime_download_preserves_legacy_cache_and_reuses_verified_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            for machine in ("x86_64", "aarch64"):
                with self.subTest(machine=machine):
                    cache_dir = Path(directory) / machine
                    cache_dir.mkdir()
                    legacy_target = cache_dir / f"appimage-runtime-{machine}"
                    legacy_bytes = f"legacy runtime {machine}".encode()
                    legacy_target.write_bytes(legacy_bytes)

                    downloaded_bytes = f"pinned runtime {machine}".encode()
                    target = cache_dir / (
                        f"appimage-runtime-{appimage_utils.APPIMAGE_RUNTIME_VERSION}-{machine}"
                    )
                    expected_url = (
                        "https://github.com/AppImage/type2-runtime/releases/"
                        f"download/continuous/runtime-{machine}"
                    )

                    def retrieve(
                        url: str,
                        destination: str,
                        expected_url: str = expected_url,
                        downloaded_bytes: bytes = downloaded_bytes,
                    ):
                        self.assertEqual(expected_url, url)
                        Path(destination).write_bytes(downloaded_bytes)
                        return destination, None

                    expected_digest = hashlib.sha256(downloaded_bytes).hexdigest()
                    with (
                        patch.dict(
                            appimage_utils.APPIMAGE_RUNTIME_SHA256,
                            {machine: expected_digest},
                        ),
                        patch.object(
                            appimage_utils.urllib.request,
                            "urlretrieve",
                            side_effect=retrieve,
                        ) as mocked_retrieve,
                    ):
                        selected = appimage_utils.download_appimage_runtime(cache_dir, machine)
                        self.assertEqual(target, selected)
                        self.assertEqual(downloaded_bytes, selected.read_bytes())
                        self.assertEqual(legacy_bytes, legacy_target.read_bytes())
                        self.assertFalse(target.with_suffix(f"{target.suffix}.download").exists())
                        mocked_retrieve.assert_called_once()

                        reused = appimage_utils.download_appimage_runtime(cache_dir, machine)
                        self.assertEqual(target, reused)
                        self.assertEqual(downloaded_bytes, reused.read_bytes())
                        self.assertEqual(legacy_bytes, legacy_target.read_bytes())
                        mocked_retrieve.assert_called_once()

    def test_bad_runtime_download_cleans_temporary_file_without_publishing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            machine = "x86_64"
            target = cache_dir / (
                f"appimage-runtime-{appimage_utils.APPIMAGE_RUNTIME_VERSION}-{machine}"
            )
            temporary_target = target.with_suffix(f"{target.suffix}.download")
            downloaded_bytes = b"bad runtime bytes"

            def retrieve(url: str, destination: str):
                Path(destination).write_bytes(downloaded_bytes)
                return destination, None

            expected_digest = hashlib.sha256(b"expected runtime bytes").hexdigest()
            with (
                patch.dict(
                    appimage_utils.APPIMAGE_RUNTIME_SHA256,
                    {machine: expected_digest},
                ),
                patch.object(
                    appimage_utils.urllib.request,
                    "urlretrieve",
                    side_effect=retrieve,
                ) as mocked_retrieve,
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    appimage_utils.download_appimage_runtime(cache_dir, machine)

            mocked_retrieve.assert_called_once()
            self.assertFalse(target.exists())
            self.assertFalse(temporary_target.exists())

    def test_corrupt_cached_runtime_is_preserved_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            machine = "x86_64"
            target = cache_dir / (
                f"appimage-runtime-{appimage_utils.APPIMAGE_RUNTIME_VERSION}-{machine}"
            )
            corrupt_bytes = b"corrupt cached runtime"
            target.write_bytes(corrupt_bytes)
            expected_digest = hashlib.sha256(b"expected runtime bytes").hexdigest()

            with (
                patch.dict(
                    appimage_utils.APPIMAGE_RUNTIME_SHA256,
                    {machine: expected_digest},
                ),
                patch.object(appimage_utils.urllib.request, "urlretrieve") as mocked_retrieve,
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    appimage_utils.download_appimage_runtime(cache_dir, machine)

            mocked_retrieve.assert_not_called()
            self.assertEqual(corrupt_bytes, target.read_bytes())
            self.assertFalse(target.with_suffix(".download").exists())

    def test_unsupported_runtime_architecture_does_not_create_cache_or_use_network(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "cache"
            with patch.object(appimage_utils.urllib.request, "urlretrieve") as mocked_retrieve:
                with self.assertRaisesRegex(RuntimeError, "Unsupported AppImage"):
                    appimage_utils.download_appimage_runtime(cache_dir, "riscv64")

            mocked_retrieve.assert_not_called()
            self.assertFalse(cache_dir.exists())

    def test_run_uses_verified_runtime_file_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_root = Path(directory)
            cache_dir = repo_root / ".appimage-tools"
            cache_dir.mkdir()
            machine = "x86_64"
            runtime_path = cache_dir / (
                f"appimage-runtime-{appimage_utils.APPIMAGE_RUNTIME_VERSION}-{machine}"
            )
            runtime_bytes = b"verified runtime for run invocation"
            runtime_path.write_bytes(runtime_bytes)
            appimagetool = repo_root / "appimagetool"
            appdir = repo_root / "AppDir"
            output_path = repo_root / "dist" / "Pandrator.AppImage"

            def fake_run(command, cwd, env):
                output_path.write_bytes(b"synthetic AppImage output")

            with (
                patch.dict(
                    appimage_utils.APPIMAGE_RUNTIME_SHA256,
                    {machine: hashlib.sha256(runtime_bytes).hexdigest()},
                ),
                patch.object(appimage_utils, "run", side_effect=fake_run) as mocked_run,
                patch.object(appimage_utils.urllib.request, "urlretrieve") as mocked_retrieve,
            ):
                appimage_utils.run_appimagetool(
                    appimagetool, appdir, output_path, repo_root, machine
                )

            mocked_retrieve.assert_not_called()
            mocked_run.assert_called_once()
            self.assertEqual(
                [
                    str(appimagetool),
                    "--runtime-file",
                    str(runtime_path),
                    str(appdir),
                    str(output_path),
                ],
                mocked_run.call_args.args[0],
            )
            self.assertEqual(repo_root, mocked_run.call_args.kwargs["cwd"])
            self.assertEqual(machine, mocked_run.call_args.kwargs["env"]["ARCH"])
            self.assertTrue(output_path.exists())

    def test_runtime_input_failure_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            repo_root = Path(directory)
            cache_dir = repo_root / ".appimage-tools"
            cache_dir.mkdir()
            machine = "x86_64"
            runtime_path = cache_dir / (
                f"appimage-runtime-{appimage_utils.APPIMAGE_RUNTIME_VERSION}-{machine}"
            )
            runtime_path.write_bytes(b"corrupt cached runtime")
            output_path = repo_root / "dist" / "Pandrator.AppImage"
            output_path.parent.mkdir()
            original_output = b"preexisting AppImage output"
            output_path.write_bytes(original_output)

            with (
                patch.dict(
                    appimage_utils.APPIMAGE_RUNTIME_SHA256,
                    {machine: hashlib.sha256(b"expected runtime").hexdigest()},
                ),
                patch.object(appimage_utils.urllib.request, "urlretrieve") as mocked_retrieve,
                patch.object(appimage_utils, "run") as mocked_run,
            ):
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    appimage_utils.run_appimagetool(
                        repo_root / "appimagetool",
                        repo_root / "AppDir",
                        output_path,
                        repo_root,
                        machine,
                    )

            mocked_retrieve.assert_not_called()
            mocked_run.assert_not_called()
            self.assertEqual(original_output, output_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
