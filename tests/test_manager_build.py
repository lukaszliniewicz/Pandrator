from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from pandrator_manager.releases.trust import TrustStore
from scripts.build_manager_appimage import (
    qualify_linux_build_host,
    stage_appdir,
    write_checksum,
)
from scripts.build_manager_bootstrap import (
    _build_temporary_wheel,
    _extract_wheel,
    _stage_wheel_source,
    _verify_wheel_provenance,
    _wheel_from_directory,
)
from scripts.build_manager_release_bundle import _release_platform
from scripts.build_release_checksums import (
    checksum_manifest,
    release_assets,
    write_checksum_manifest,
)
from scripts.qualify_manager_lifecycle import _default_bundle_path


class ManagerBootstrapBuildTests(unittest.TestCase):
    def test_temporary_wheel_build_runs_from_clean_staged_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staged = root / "staged"
            staged.mkdir()
            destination = root / "wheel"
            expected = destination / "pandrator-0.8.16-py3-none-any.whl"
            with (
                mock.patch(
                    "scripts.build_manager_bootstrap._stage_wheel_source",
                    return_value=staged,
                ),
                mock.patch(
                    "scripts.build_manager_bootstrap._wheel_from_directory",
                    return_value=expected,
                ),
                mock.patch("scripts.build_manager_bootstrap.subprocess.run") as run,
            ):
                wheel = _build_temporary_wheel(root, destination)

            self.assertEqual(expected, wheel)
            arguments = run.call_args.args[0]
            self.assertNotIn("--no-isolation", arguments)
            self.assertEqual(staged, run.call_args.kwargs["cwd"])
            self.assertTrue(run.call_args.kwargs["check"])

    def test_manager_appimage_rejects_a_newer_glibc_build_floor(self) -> None:
        with (
            mock.patch("scripts.build_manager_appimage.sys.platform", "linux"),
            mock.patch(
                "scripts.build_manager_appimage._glibc_version",
                return_value=(2, 39),
            ),
            self.assertRaisesRegex(RuntimeError, "maximum qualified"),
        ):
            qualify_linux_build_host()

        with (
            mock.patch("scripts.build_manager_appimage.sys.platform", "linux"),
            mock.patch(
                "scripts.build_manager_appimage._glibc_version",
                return_value=(2, 35),
            ),
        ):
            qualify_linux_build_host()

    def test_release_checksum_manifest_is_single_sorted_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            second = root / "second.zip"
            first = root / "first.exe"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            (root / "first.exe.sha256").write_text(
                "legacy sidecar",
                encoding="ascii",
            )
            (root / "release-notes.md").write_text(
                "notes",
                encoding="utf-8",
            )

            selected = release_assets(root)
            output = write_checksum_manifest(
                selected,
                root / "SHA256SUMS",
            )

            self.assertEqual(
                [path.name for path in selected],
                ["first.exe", "second.zip"],
            )
            self.assertEqual(
                output.read_text(encoding="ascii"),
                checksum_manifest((second, first)),
            )
            self.assertEqual(
                output.read_text(encoding="ascii").splitlines(),
                [
                    f"{hashlib.sha256(b'first').hexdigest()}  first.exe",
                    f"{hashlib.sha256(b'second').hexdigest()}  second.zip",
                ],
            )

    def test_windows_bootstrap_uses_the_gui_subsystem(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        specification = (repository / "pandrator_manager_bootstrap.spec").read_text(
            encoding="utf-8"
        )

        self.assertIn('console=os.name != "nt"', specification)
        self.assertIn('collect_submodules("dbus_next")', specification)

    def test_tray_logo_is_included_in_the_wheel_and_frozen_bootstrap(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        specification = (repository / "pandrator_manager_bootstrap.spec").read_text(
            encoding="utf-8"
        )
        package_configuration = (
            repository / "pandrator_manager" / "pyproject.toml"
        ).read_text(encoding="utf-8")

        self.assertIn('tray" / "pandrator-tray.png"', specification)
        self.assertIn('"pandrator_manager.tray"', package_configuration)
        self.assertIn('"pandrator-tray.png"', package_configuration)

    def test_release_bundle_uses_public_platform_names(self) -> None:
        self.assertEqual(
            _release_platform(system="win32", machine="AMD64"),
            "windows-x86_64",
        )
        self.assertEqual(
            _release_platform(system="linux", machine="x86_64"),
            "linux-x86_64",
        )

    def test_lifecycle_qualification_uses_the_release_bundle_name(self) -> None:
        root = Path("/release")

        self.assertEqual(
            _default_bundle_path(
                root,
                "0.9.0",
                system="win32",
                machine="AMD64",
            ),
            root / "dist" / "pandrator-manager-0.9.0-windows-x86_64.zip",
        )
        self.assertEqual(
            _default_bundle_path(
                root,
                "0.9.0",
                system="linux",
                machine="x86_64",
            ),
            root / "dist" / "pandrator-manager-0.9.0-linux-x86_64.zip",
        )

    def test_release_signer_signs_the_normalized_document_it_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            private = Ed25519PrivateKey.generate()
            public = base64.b64encode(
                private.public_key().public_bytes(
                    Encoding.Raw,
                    PublicFormat.Raw,
                )
            ).decode("ascii")
            key = root / "release-key.json"
            key.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "algorithm": "ed25519",
                        "key_id": "qualification",
                        "private_key": base64.b64encode(
                            private.private_bytes(
                                Encoding.Raw,
                                PrivateFormat.Raw,
                                NoEncryption(),
                            )
                        ).decode("ascii"),
                        "public_key": public,
                    }
                ),
                encoding="utf-8",
            )

            bundles: list[Path] = []
            for system in ("windows", "linux"):
                bundle = root / f"manager-{system}.zip"
                with zipfile.ZipFile(bundle, "w") as archive:
                    archive.writestr(
                        "pandrator-release.json",
                        json.dumps(
                            {
                                "product": "pandrator-manager",
                                "version": "0.9.0",
                                "runtime_kind": "native_launcher",
                            }
                        ),
                    )
                bundles.append(bundle)

            output = root / "release.json"
            repository = Path(__file__).resolve().parents[1]
            subprocess.run(
                [
                    sys.executable,
                    str(repository / "scripts" / "sign_manager_release.py"),
                    "--key",
                    str(key),
                    "--version",
                    "0.9.0",
                    "--sequence",
                    "1",
                    "--release-tag",
                    "v0.9.0",
                    "--windows-bundle",
                    str(bundles[0]),
                    "--linux-bundle",
                    str(bundles[1]),
                    "--output",
                    str(output),
                ],
                cwd=repository,
                check=True,
                stdout=subprocess.PIPE,
                text=True,
            )

            verified = TrustStore({"qualification": public}).verify(output.read_bytes())
            self.assertEqual(verified.payload.version, "0.9.0")
            self.assertEqual(
                output.with_suffix(".json.sha256").read_text(encoding="ascii"),
                f"{hashlib.sha256(output.read_bytes()).hexdigest()}  release.json\n",
            )

    def test_wheel_source_staging_excludes_local_build_residue(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "pandrator_manager"
            source.mkdir()
            (source / "pyproject.toml").write_text(
                "[build-system]\nrequires = []\n",
                encoding="utf-8",
            )
            (source / "__init__.py").write_text("", encoding="utf-8")
            (source / "build" / "lib").mkdir(parents=True)
            (source / "build" / "lib" / "stale.pyc").write_bytes(b"stale")
            (source / "__pycache__").mkdir()
            (source / "__pycache__" / "stale.pyc").write_bytes(b"stale")
            egg_info = source / "pandrator_manager.egg-info"
            egg_info.mkdir()
            (egg_info / "SOURCES.txt").write_text("stale", encoding="utf-8")
            virtual_environment = source / ".venv"
            virtual_environment.mkdir()
            (virtual_environment / "local.txt").write_text("local", encoding="utf-8")

            staged = _stage_wheel_source(root, root / "staged")

            self.assertTrue((staged / "pyproject.toml").is_file())
            self.assertTrue((staged / "__init__.py").is_file())
            self.assertFalse((staged / "build").exists())
            self.assertFalse((staged / "__pycache__").exists())
            self.assertFalse((staged / "pandrator_manager.egg-info").exists())
            self.assertFalse((staged / ".venv").exists())

    def test_wheel_extraction_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "pandrator_manager-0.6.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("../outside.py", b"unsafe")

            with self.assertRaisesRegex(RuntimeError, "unsafe member"):
                _extract_wheel(wheel, root / "site")

            self.assertFalse((root / "outside.py").exists())

    def test_wheel_extraction_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            wheel = root / "pandrator_manager-0.6.0-py3-none-any.whl"
            link = zipfile.ZipInfo("pandrator_manager/redirect")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(link, b"../../outside")

            with self.assertRaisesRegex(RuntimeError, "non-regular member"):
                _extract_wheel(wheel, root / "site")

    def test_wheel_directory_requires_one_manager_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(RuntimeError, "found 0"):
                _wheel_from_directory(root)

            first = root / "pandrator_manager-0.6.0-py3-none-any.whl"
            first.write_bytes(b"wheel")
            self.assertEqual(_wheel_from_directory(root), first.resolve())

            (root / "pandrator_manager-0.6.1-py3-none-any.whl").write_bytes(b"wheel")
            with self.assertRaisesRegex(RuntimeError, "found 2"):
                _wheel_from_directory(root)

    def test_provenance_rejects_checkout_manager_source(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "pandrator_manager"
            wheel = root / "wheel" / "pandrator_manager"
            analysis = root / "build" / "pandrator_manager_bootstrap"
            source.mkdir(parents=True)
            wheel.mkdir(parents=True)
            analysis.mkdir(parents=True)
            source_launcher = source / "launcher.py"
            source_launcher.write_text("", encoding="utf-8")
            wheel_launcher = wheel / "launcher.py"
            wheel_launcher.write_text("", encoding="utf-8")
            (analysis / "Analysis-00.toc").write_text(
                repr((str(wheel_launcher), str(source_launcher))),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "checkout"):
                _verify_wheel_provenance(root, wheel.parent)

    @unittest.skipIf(
        os.name == "nt",
        "Executable mode checks require POSIX permission semantics.",
    )
    def test_manager_appdir_is_qt_free_and_launches_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "build").mkdir()
            (root / "pandrator.png").write_bytes(b"png")
            bootstrap = root / "bootstrap"
            bootstrap.write_bytes(b"manager")
            bootstrap.chmod(bootstrap.stat().st_mode | stat.S_IXUSR)
            appdir = root / "build" / "PandratorManager.AppDir"

            stage_appdir(root, bootstrap, appdir)

            apprun = appdir / "AppRun"
            desktop = appdir / "io.github.lukaszliniewicz.PandratorManager.desktop"
            metainfo = (
                appdir
                / "usr"
                / "share"
                / "metainfo"
                / "io.github.lukaszliniewicz.PandratorManager.appdata.xml"
            )
            executable = appdir / "usr" / "bin" / "pandrator-manager-launcher"
            self.assertTrue(apprun.stat().st_mode & stat.S_IXUSR)
            self.assertTrue(executable.stat().st_mode & stat.S_IXUSR)
            self.assertIn(
                'exec "$HERE/usr/bin/pandrator-manager-launcher" "$@"',
                apprun.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "Name=Pandrator Manager",
                desktop.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "<project_license>MIT</project_license>",
                metainfo.read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "Qt",
                "\n".join(path.name for path in appdir.rglob("*")),
            )

    def test_manager_appimage_checksum_is_machine_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            artifact = Path(raw) / "PandratorManager-x86_64.AppImage"
            artifact.write_bytes(b"appimage")

            checksum, digest = write_checksum(artifact)

            self.assertEqual(
                checksum.read_text(encoding="ascii"),
                f"{digest}  {artifact.name}\n",
            )


if __name__ == "__main__":
    unittest.main()
