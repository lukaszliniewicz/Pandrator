import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pandrator_installer.service import HeadlessInstaller


class InstallerUpdateMigrationTests(unittest.TestCase):
    def test_legacy_rvc_package_enables_service_migration(self):
        with tempfile.TemporaryDirectory() as install_root:
            site_packages = os.path.join(
                install_root,
                "envs",
                "pandrator_installer",
                ".pixi",
                "envs",
                "default",
                "Lib",
                "site-packages",
            )
            os.makedirs(os.path.join(site_packages, "rvc_python"))
            installer = HeadlessInstaller(working_dir=os.path.dirname(install_root))

            config = installer.ensure_rvc_support_flag(install_root, {})

            self.assertTrue(config["rvc_support"])

    def test_rvc_service_is_prepared_before_legacy_packages_are_removed(self):
        with tempfile.TemporaryDirectory() as install_root:
            pandrator_repo = os.path.join(install_root, "Pandrator")
            rvc_repo = os.path.join(install_root, "rvc-python")
            installer = HeadlessInstaller(working_dir=os.path.dirname(install_root))
            events = []

            with patch.object(installer, "is_rvc_runtime_ready", return_value=False), \
                 patch.object(
                     installer,
                     "install_rvc_api_server",
                     side_effect=lambda *_args, **_kwargs: events.append("prepare"),
                 ), \
                 patch.object(
                     installer,
                     "remove_legacy_rvc_from_pandrator_env",
                     side_effect=lambda *_args, **_kwargs: events.append("remove"),
                 ):
                installer.migrate_rvc_to_service(
                    install_root,
                    pandrator_repo,
                    rvc_repo,
                    pixi_path="pixi.exe",
                )

            self.assertEqual(events, ["prepare", "remove"])
            self.assertTrue(os.path.isdir(os.path.join(pandrator_repo, "rvc_models")))

    @patch("pandrator_installer.service.HeadlessInstaller.install_requirement_specs_with_pip")
    @patch("pandrator_installer.service.HeadlessInstaller.add_pypi_requirements", return_value=[])
    @patch("pandrator_installer.service.HeadlessInstaller.add_pixi_conda_package")
    @patch("pandrator_installer.service.HeadlessInstaller.run_pixi_in_env")
    def test_missing_nemo_runtime_is_repaired_explicitly(
        self,
        run_pixi,
        add_conda,
        add_pypi,
        install_with_pip,
    ):
        installer = HeadlessInstaller(working_dir="workspace")
        run_pixi.side_effect = [
            subprocess.CalledProcessError(1, ["python"], stderr="missing"),
            ("", ""),
        ]

        installer.ensure_nemo_text_processing_runtime("C:/Pandrator")

        add_conda.assert_called_once()
        add_pypi.assert_called_once()
        install_with_pip.assert_not_called()
        self.assertEqual(run_pixi.call_count, 2)

    @patch("pandrator_installer.service.HeadlessInstaller.install_requirement_specs_with_pip")
    @patch("pandrator_installer.service.HeadlessInstaller.add_pypi_requirements", return_value=[])
    @patch("pandrator_installer.service.HeadlessInstaller.run_pixi_in_env")
    def test_missing_wtpsplit_runtime_is_repaired_and_verified(
        self,
        run_pixi,
        add_pypi,
        install_with_pip,
    ):
        installer = HeadlessInstaller(working_dir="workspace")
        run_pixi.side_effect = [
            subprocess.CalledProcessError(1, ["python"], stderr="missing"),
            ("", ""),
        ]

        installer.ensure_wtpsplit_runtime("C:/Pandrator")

        add_pypi.assert_called_once_with(
            "C:/Pandrator",
            "pandrator_installer",
            ["numpy==1.26.4", "wtpsplit-lite==0.2.0"],
        )
        install_with_pip.assert_not_called()
        self.assertEqual(run_pixi.call_count, 2)

    @patch("pandrator_installer.components.psutil.process_iter")
    def test_update_refuses_to_mutate_a_running_installation(self, process_iter):
        installer = HeadlessInstaller(working_dir="workspace")
        process_iter.return_value = [
            SimpleNamespace(
                pid=1234,
                info={
                    "name": "python.exe",
                    "exe": os.path.abspath("C:/Pandrator/envs/pandrator_installer/python.exe"),
                },
            )
        ]

        with self.assertRaisesRegex(RuntimeError, "Close Pandrator"):
            installer.ensure_update_runtime_stopped(os.path.abspath("C:/Pandrator"))


if __name__ == "__main__":
    unittest.main()
