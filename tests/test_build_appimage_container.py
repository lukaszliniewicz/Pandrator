import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_appimage_container as builder
from scripts import build_linux_appimage as linux_builder


class AppImageContainerBuilderTests(unittest.TestCase):
    def test_appimagetool_checksum_rejects_changed_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            tool_path = Path(directory) / "appimagetool.AppImage"
            tool_path.write_bytes(b"expected appimagetool")
            expected = linux_builder.sha256_file(tool_path)

            with patch.dict(
                linux_builder.APPIMAGETOOL_SHA256,
                {"x86_64": expected},
            ):
                linux_builder.verify_appimagetool(tool_path, "x86_64")
                tool_path.write_bytes(b"changed appimagetool")
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    linux_builder.verify_appimagetool(tool_path, "x86_64")

    def test_container_fingerprint_changes_with_build_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            container_dir = Path(directory)
            (container_dir / "Dockerfile").write_text("FROM debian:11-slim\n", encoding="utf-8")
            (container_dir / "build.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            first = builder.container_fingerprint(container_dir)

            (container_dir / "build.sh").write_text("#!/bin/sh\nset -eu\n", encoding="utf-8")
            second = builder.container_fingerprint(container_dir)

        self.assertNotEqual(first, second)
        self.assertEqual(12, len(first))

    def test_image_reference_rejects_user_supplied_tag(self):
        with self.assertRaisesRegex(RuntimeError, "must not include a tag"):
            builder.image_reference("example/pandrator:latest")

    def test_build_command_uses_selected_platform_and_context(self):
        command = builder.build_image_command(
            "podman",
            "pandrator-builder:abc123",
            "linux/amd64",
            builder.CONTAINER_DIR,
            no_cache=True,
        )

        self.assertEqual("podman", command[0])
        self.assertIn("linux/amd64", command)
        self.assertIn("--no-cache", command)
        self.assertEqual(str(builder.CONTAINER_DIR), command[-1])

    def test_run_command_mounts_source_read_only_and_output_writable(self):
        checkout = Path.cwd().resolve()
        artifacts = (checkout / "dist").resolve()
        command = builder.container_run_command(
            "docker",
            "pandrator-builder:abc123",
            "linux/amd64",
            checkout,
            artifacts,
            ["--no-network-smoke-test"],
            user_spec="1000:1000",
        )

        self.assertIn(f"{checkout}:/source:ro", command)
        self.assertIn(f"{artifacts}:/output", command)
        self.assertIn("1000:1000", command)
        self.assertEqual("--no-network-smoke-test", command[-1])

    def test_podman_run_disables_selinux_labels_without_relabeling_source(self):
        command = builder.container_run_command(
            "/usr/bin/podman",
            "pandrator-builder:abc123",
            "linux/amd64",
            Path("/checkout"),
            Path("/artifacts"),
            [],
            user_spec="1000:1000",
        )

        self.assertIn("--security-opt", command)
        self.assertIn("label=disable", command)
        self.assertIn("--userns", command)
        self.assertIn("keep-id", command)
        self.assertNotIn("1000:1000", command)
        self.assertNotIn("/checkout:/source:ro,Z", command)

    @patch("scripts.build_appimage_container.shutil.which")
    @patch("scripts.build_appimage_container.subprocess.run")
    def test_runtime_auto_falls_back_to_podman(self, run_mock, which_mock):
        which_mock.side_effect = lambda name: f"/usr/bin/{name}"
        run_mock.side_effect = [
            unittest.mock.Mock(returncode=1),
            unittest.mock.Mock(returncode=0),
        ]

        runtime = builder.resolve_runtime("auto")

        self.assertEqual("/usr/bin/podman", runtime)
        self.assertEqual(2, run_mock.call_count)


if __name__ == "__main__":
    unittest.main()
