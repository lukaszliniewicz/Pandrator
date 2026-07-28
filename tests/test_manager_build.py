from __future__ import annotations

import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.build_manager_appimage import stage_appdir, write_checksum
from scripts.build_manager_bootstrap import (
    _extract_wheel,
    _stage_wheel_source,
    _verify_wheel_provenance,
    _wheel_from_directory,
)
from scripts.build_manager_release_bundle import _release_platform


class ManagerBootstrapBuildTests(unittest.TestCase):
    def test_release_bundle_uses_public_platform_names(self) -> None:
        self.assertEqual(
            _release_platform(system="win32", machine="AMD64"),
            "windows-x86_64",
        )
        self.assertEqual(
            _release_platform(system="linux", machine="x86_64"),
            "linux-x86_64",
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

            staged = _stage_wheel_source(root, root / "staged")

            self.assertTrue((staged / "pyproject.toml").is_file())
            self.assertTrue((staged / "__init__.py").is_file())
            self.assertFalse((staged / "build").exists())
            self.assertFalse((staged / "__pycache__").exists())
            self.assertFalse(
                (staged / "pandrator_manager.egg-info").exists()
            )

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

            (root / "pandrator_manager-0.6.1-py3-none-any.whl").write_bytes(
                b"wheel"
            )
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
            desktop = (
                appdir
                / "io.github.lukaszliniewicz.PandratorManager.desktop"
            )
            metainfo = (
                appdir
                / "usr"
                / "share"
                / "metainfo"
                / "io.github.lukaszliniewicz.PandratorManager.appdata.xml"
            )
            executable = (
                appdir
                / "usr"
                / "bin"
                / "pandrator-manager-launcher"
            )
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
                "<project_license>AGPL-3.0-only</project_license>",
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
