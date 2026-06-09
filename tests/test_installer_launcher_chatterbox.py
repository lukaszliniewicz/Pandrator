import unittest
from unittest.mock import MagicMock, patch
import os
from PyQt6.QtWidgets import QApplication, QCheckBox
from pandrator_installer_launcher import PandratorInstaller


class TestInstallerLauncherChatterbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize QApplication offscreen for testing PyQt widgets
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_chatterbox_checkbox_mutual_exclusion(self):
        installer = PandratorInstaller(headless=True)
        # Ensure the widgets exist
        self.assertTrue(hasattr(installer, "chatterbox_checkbox"))
        self.assertTrue(hasattr(installer, "chatterbox_cpu_checkbox"))

        # Verify mutual exclusion binding works
        # Check Chatterbox GPU checkbox
        installer.chatterbox_checkbox.setChecked(True)
        self.assertTrue(installer.chatterbox_checkbox.isChecked())
        self.assertFalse(installer.chatterbox_cpu_checkbox.isChecked())

        # Check Chatterbox CPU checkbox, should uncheck GPU checkbox
        installer.chatterbox_cpu_checkbox.setChecked(True)
        self.assertTrue(installer.chatterbox_cpu_checkbox.isChecked())
        self.assertFalse(installer.chatterbox_checkbox.isChecked())

        # Check Chatterbox GPU checkbox again, should uncheck CPU checkbox
        installer.chatterbox_checkbox.setChecked(True)
        self.assertTrue(installer.chatterbox_checkbox.isChecked())
        self.assertFalse(installer.chatterbox_cpu_checkbox.isChecked())

    @patch("pandrator_installer_launcher.PandratorInstaller.load_install_config")
    @patch("pandrator_installer_launcher.PandratorInstaller.execute_concurrently")
    @patch("pandrator_installer_launcher.PandratorInstaller.check_pixi")
    @patch("pandrator_installer_launcher.PandratorInstaller.get_pixi_executable")
    @patch("pandrator_installer_launcher.PandratorInstaller.create_pixi_env")
    @patch("pandrator_installer_launcher.PandratorInstaller.add_pixi_conda_package")
    @patch("pandrator_installer_launcher.PandratorInstaller.run_pixi_command")
    @patch("pandrator_installer_launcher.PandratorInstaller.ensure_pandrator_runtime")
    @patch("pandrator_installer_launcher.PandratorInstaller.should_install_requirements")
    @patch("pandrator_installer_launcher.PandratorInstaller.install_subdub_requirements")
    @patch("pandrator_installer_launcher.PandratorInstaller.is_chatterbox_runtime_ready")
    @patch("pandrator_installer_launcher.PandratorInstaller.install_chatterbox_api_server")
    @patch("pandrator_installer_launcher.PandratorInstaller.write_packaging_layout")
    @patch("pandrator_installer_launcher.PandratorInstaller.component_needs_package_sync")
    @patch("pandrator_installer_launcher.PandratorInstaller.rvc_needs_package_sync")
    def test_chatterbox_update_process_flow(
        self,
        mock_rvc_sync,
        mock_comp_sync,
        mock_write_layout,
        mock_install_chatterbox,
        mock_runtime_ready,
        mock_subdub_reqs,
        mock_should_install_reqs,
        mock_ensure_runtime,
        mock_run_pixi_cmd,
        mock_add_pixi_package,
        mock_create_pixi,
        mock_get_pixi,
        mock_check_pixi,
        mock_execute_concurrently,
        mock_load_config,
    ):
        installer = PandratorInstaller(headless=True)
        installer.worker = MagicMock()

        # 1. Config says Chatterbox is supported
        mock_load_config.return_value = {
            "chatterbox_support": True,
            "chatterbox_gpu_support": True,
        }
        mock_check_pixi.return_value = True
        mock_get_pixi.return_value = "dummy_pixi"
        mock_should_install_reqs.return_value = (False, "dummy")
        mock_comp_sync.return_value = (False, "dummy")
        mock_rvc_sync.return_value = (False, "dummy")
        # Runtime is NOT ready, so bootstrapping should trigger
        mock_runtime_ready.return_value = False

        # Run update process
        with patch.object(installer, "pull_repo") as mock_pull, \
             patch.object(installer, "clone_repo") as mock_clone, \
             patch.object(installer, "backup_state_database") as mock_backup, \
             patch("os.path.exists", return_value=True) as mock_exists:
            
            mock_backup.return_value = []
            installer.update_process()

            # Verify that update_tasks has registered Chatterbox pull/clone
            mock_execute_concurrently.assert_called_once()
            tasks = mock_execute_concurrently.call_args[0][0]
            
            # Either "Update Chatterbox" or "Clone Chatterbox" must be in the tasks dictionary.
            self.assertTrue(
                "Update Chatterbox" in tasks or "Clone Chatterbox" in tasks,
                "Chatterbox update/clone task was not registered in update_tasks"
            )

            # Verify that bootstrapping was called because runtime is not ready
            mock_install_chatterbox.assert_called_once()
            called_args, called_kwargs = mock_install_chatterbox.call_args
            # Since GPU support is True, use_cpu should be False
            self.assertFalse(called_kwargs["use_cpu"])


if __name__ == "__main__":
    unittest.main()
