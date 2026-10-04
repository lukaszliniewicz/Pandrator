"""Native update ownership and pip destination regressions in disposable roots."""

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import venv
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pandrator_installer.lifecycle import main
from pandrator_installer.models import WorkspacePaths
from pandrator_installer.update import _site_packages, install_wheel


def signed_release(workspace: Path, wheel: Path) -> tuple[Path, Path]:
    private = Ed25519PrivateKey.generate()
    public = workspace / "release-public.pem"
    public.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    signed = {
        "version": "0.49.0",
        "wheel": {"filename": wheel.name, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
    }
    canonical = json.dumps(signed, sort_keys=True, separators=(",", ":")).encode()
    manifest = workspace / "release.json"
    manifest.write_text(
        json.dumps(
            {"signed": signed, "signature": base64.b64encode(private.sign(canonical)).decode()}
        )
    )
    return manifest, public


def make_environment(paths: WorkspacePaths) -> Path:
    environment = paths.pandrator_repo / ".pixi" / "envs" / "default"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    return environment / "bin" / "python"


def pip_source() -> Path | None:
    spec = importlib.util.find_spec("pip")
    if spec is not None and spec.origin is not None:
        return Path(spec.origin).parent
    host = Path("/usr/bin/python3")
    if not host.is_file():
        return None
    query = subprocess.run(
        [
            str(host),
            "-c",
            "import importlib.util; s=importlib.util.find_spec('pip'); print(s.origin if s else '')",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return Path(query.stdout.strip()).parent if query.stdout.strip() else None


def probe_wheel(
    workspace: Path,
    *,
    package: str = "pandrator_update_probe",
    version: str = "0.0.1",
    value: str = "owned-update-witness",
) -> Path:
    wheel = workspace / f"{package}-{version}-py3-none-any.whl"
    metadata = f"{package}-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}/__init__.py", f"VALUE = {value!r}\n")
        archive.writestr(
            metadata + "/METADATA",
            f"Metadata-Version: 2.1\nName: {package.replace('_', '-')}\nVersion: {version}\n",
        )
        archive.writestr(
            metadata + "/WHEEL",
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(metadata + "/RECORD", "")
    return wheel


def make_standalone_environment(paths: WorkspacePaths) -> Path:
    prefix = paths.pandrator_repo / ".pixi" / "envs" / "default"
    python = prefix / "bin" / "python"
    python.parent.mkdir(parents=True)
    shutil.copy2(sys.executable, python)
    library = Path(str(sysconfig.get_config_var("LIBDIR"))) / str(
        sysconfig.get_config_var("LDLIBRARY")
    )
    if library.is_file() and ".so" in library.name:
        (prefix / "lib").mkdir(exist_ok=True)
        shutil.copy2(library, prefix / "lib" / library.name)
    stdlib = prefix / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
    shutil.copytree(
        Path(sysconfig.get_path("stdlib")),
        stdlib,
        ignore=shutil.ignore_patterns("site-packages", "__pycache__", "test", "tests"),
    )
    (stdlib / "site-packages").mkdir()
    return python


@unittest.skipUnless(sys.platform.startswith("linux"), "Native Linux environment fixtures")
class InstallerUpdateEnvironmentTests(unittest.TestCase):
    def invoke(self, workspace: Path, wheel: Path) -> tuple[int, str]:
        manifest, public = signed_release(workspace, wheel)
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            code = main(
                [
                    "update",
                    "--workspace",
                    str(workspace),
                    "--wheel",
                    str(wheel),
                    "--manifest",
                    str(manifest),
                    "--public-key",
                    str(public),
                    "--json",
                ]
            )
        return code, output.getvalue() + error.getvalue()

    def test_unowned_environment_refuses_before_stop_marker_backup_or_activation(self):
        native_popen = subprocess.Popen
        for case in ("absent", "bare-symlink", "escaped-site"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                paths = WorkspacePaths.from_value(directory)
                paths.install_root.mkdir()
                if case == "bare-symlink":
                    python = paths.pandrator_repo / ".pixi" / "envs" / "default" / "bin" / "python"
                    python.parent.mkdir(parents=True)
                    python.symlink_to(sys.executable)
                elif case == "escaped-site":
                    python = make_environment(paths)
                    site = _site_packages(python)
                    site.rmdir()
                    outside = paths.workspace / "outside-site"
                    outside.mkdir()
                    site.symlink_to(outside, target_is_directory=True)
                state = paths.install_root / "runtime-processes.json"
                before = b"{}"
                state.write_bytes(before)
                wheel = paths.workspace / "pandrator-0.49.0-py3-none-any.whl"
                wheel.write_bytes(b"signed fixture, activation stubbed")

                def start(command, *args, **kwargs):
                    if command == ["fixture-launcher"]:
                        return Mock()
                    return native_popen(command, *args, **kwargs)

                with (
                    patch("pandrator_installer.lifecycle._validated_supervisor_process") as owner,
                    patch("pandrator_installer.lifecycle.snapshot_installed_package") as snapshot,
                    patch("pandrator_installer.lifecycle.install_wheel") as install,
                    patch("pandrator_installer.lifecycle.run_migrations") as migrate,
                    patch("pandrator_installer.lifecycle.health_check") as health,
                    patch(
                        "pandrator_installer.lifecycle.subprocess.Popen", side_effect=start
                    ) as restart,
                ):
                    owner.return_value.cmdline.return_value = ["fixture-launcher"]
                    owner.return_value.cwd.return_value = directory
                    code, message = self.invoke(paths.workspace, wheel)
                self.assertEqual(code, 2, message)
                self.assertIn("owned by this installation", message)
                owner.assert_not_called()
                snapshot.assert_not_called()
                install.assert_not_called()
                migrate.assert_not_called()
                health.assert_not_called()
                self.assertFalse(
                    any(call.args[0] == ["fixture-launcher"] for call in restart.call_args_list)
                )
                self.assertEqual(state.read_bytes(), before)
                self.assertFalse((paths.install_root / "maintenance.json").exists())
                self.assertFalse((paths.install_root / "backups").exists())

    def test_owned_venv_with_external_base_executable_remains_updateable(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = WorkspacePaths.from_value(directory)
            python = make_environment(paths)
            self.assertFalse(python.resolve().is_relative_to(paths.install_root))
            site = _site_packages(python)
            self.assertTrue(site.is_relative_to(paths.install_root))
            wheel = paths.workspace / "pandrator-0.49.0-py3-none-any.whl"
            wheel.write_bytes(b"signed fixture, activation stubbed")
            with (
                patch(
                    "pandrator_installer.lifecycle.snapshot_installed_package", return_value=site
                ) as snapshot,
                patch("pandrator_installer.lifecycle.install_wheel") as install,
                patch("pandrator_installer.lifecycle.run_migrations"),
                patch("pandrator_installer.lifecycle.health_check"),
            ):
                code, message = self.invoke(paths.workspace, wheel)
            self.assertEqual(code, 0, message)
            self.assertEqual(snapshot.call_args.args[0], python)
            self.assertEqual(install.call_args.args[0], python)
            self.assertFalse((paths.install_root / "maintenance.json").exists())

    def test_native_pip_redirect_settings_cannot_write_outside_owned_environment(self):
        source = pip_source()
        if source is None:
            self.skipTest("No installed pip source available for disposable native fixture")
        for route in ("environment-target", "configuration-target"):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory:
                paths = WorkspacePaths.from_value(directory)
                python = make_environment(paths)
                site = _site_packages(python)
                shutil.copytree(source, site / "pip")
                wheel = probe_wheel(paths.workspace)
                outside = paths.workspace / "outside-target"
                outside.mkdir()
                witness = outside / "keep.txt"
                witness.write_bytes(b"unchanged outside installation")
                config = paths.workspace / "fixture-pip.conf"
                config.write_text(f"[install]\ntarget = {outside}\n")
                environment = {
                    name: value
                    for name, value in os.environ.items()
                    if not name.startswith("PIP_") and not name.startswith("PYTHON")
                }
                environment.update(
                    {
                        "PIP_CONFIG_FILE": str(config)
                        if route == "configuration-target"
                        else os.devnull,
                        "PIP_NO_INDEX": "1",
                        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                    }
                )
                if route == "environment-target":
                    environment["PIP_TARGET"] = str(outside)
                with patch.dict(os.environ, environment, clear=True):
                    install_wheel(python, wheel)
                self.assertEqual(sorted(path.name for path in outside.iterdir()), ["keep.txt"])
                self.assertEqual(witness.read_bytes(), b"unchanged outside installation")
                installed = site / "pandrator_update_probe" / "__init__.py"
                self.assertEqual(installed.read_text(), "VALUE = 'owned-update-witness'\n")
                self.assertTrue(
                    (site / "pandrator_update_probe-0.0.1.dist-info" / "RECORD").is_file()
                )

    def test_native_standalone_update_refuses_external_uninstall_and_accepts_owned_package(self):
        from pandrator_installer.update_environment import validate_update_environment

        source = pip_source()
        if source is None:
            self.skipTest("No installed pip source available for disposable native fixture")
        with tempfile.TemporaryDirectory() as directory:
            paths = WorkspacePaths.from_value(directory)
            python = make_standalone_environment(paths)
            selected = validate_update_environment(python, paths.install_root)
            prefixes = subprocess.run(
                [str(python), "-I", "-c", "import sys;print(sys.prefix);print(sys.base_prefix)"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout.splitlines()
            self.assertEqual(prefixes, [str(selected.prefix), str(selected.prefix)])
            site = selected.purelib
            shutil.copytree(source, site / "pip")
            old = probe_wheel(paths.workspace, package="pandrator", value="external-before")
            new = probe_wheel(
                paths.workspace, package="pandrator", version="0.0.2", value="owned-after"
            )
            outside = paths.workspace / "outside-site"
            outside.mkdir()
            environment = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("PIP_") and not name.startswith("PYTHON")
            }
            environment["PIP_CONFIG_FILE"] = os.devnull

            def seed(target: Path):
                subprocess.run(
                    [
                        str(python),
                        "-I",
                        "-m",
                        "pip",
                        "--isolated",
                        "--disable-pip-version-check",
                        "install",
                        "--no-index",
                        "--no-deps",
                        "--upgrade",
                        "--target",
                        str(target),
                        str(old),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=environment,
                )

            seed(outside)
            external = outside / "pandrator" / "__init__.py"
            before = external.read_bytes()
            expose = site / "external.pth"
            expose.write_text(str(outside) + "\n")
            (site / "pandrator").mkdir()
            (site / "pandrator" / "owned-before.txt").write_text("Owned snapshot member")
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "outside this installation"):
                    install_wheel(python, new)
                code, message = self.invoke(paths.workspace, new)
                self.assertEqual(code, 2, message)
                self.assertIn("outside this installation", message)
                self.assertFalse((paths.install_root / "maintenance.json").exists())
                self.assertFalse((paths.install_root / "backups").exists())
                self.assertEqual(external.read_bytes(), before)
                expose.unlink()
                seed(site)
                install_wheel(python, new)
            self.assertEqual(external.read_bytes(), before)
            self.assertEqual(
                (site / "pandrator" / "__init__.py").read_text(), "VALUE = 'owned-after'\n"
            )
            self.assertTrue((site / "pandrator-0.0.2.dist-info" / "RECORD").is_file())
            record = site / "pandrator-0.0.2.dist-info" / "RECORD"
            original_record = record.read_bytes()
            with record.open("a") as handle:
                handle.write(os.path.relpath(external, site) + ",,\n")
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "outside this installation"):
                    install_wheel(python, new)
            self.assertEqual(external.read_bytes(), before)
            record.write_bytes(original_record)
            secondary = selected.prefix / "secondary-site"
            legacy = secondary / "pandrator-0.0.1.egg-info"
            legacy.mkdir(parents=True)
            (legacy / "PKG-INFO").write_text(
                "Metadata-Version: 1.2\nName: pandrator\nVersion: 0.0.1\n"
            )
            (legacy / "SOURCES.txt").write_text("pandrator/__init__.py\n")
            (legacy / "installed-files.txt").write_text(os.path.relpath(external, legacy) + "\n")
            (site / "legacy.pth").write_text(f"import sys;sys.path.insert(0,{str(secondary)!r})\n")
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(RuntimeError, "ownership could not be verified"):
                    install_wheel(python, new)
            self.assertEqual(external.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
