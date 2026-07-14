import unittest
from unittest.mock import MagicMock, patch
import json
import os
import tempfile
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QLabel, QScrollArea, QSizePolicy
from pandrator_installer_launcher import PandratorInstaller
from pandrator_installer.gui.main_window import OwnerPasswordDialog, QwenConfigDialog
from pandrator_installer.gui.support import ToggleSwitch


class TestInstallerLauncherChatterbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Initialize QApplication offscreen for testing PyQt widgets
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def test_chatterbox_cpu_option_maps_to_cpu_install_variant(self):
        installer = PandratorInstaller(headless=True)
        self.assertTrue(hasattr(installer, "chatterbox_checkbox"))
        self.assertTrue(hasattr(installer, "chatterbox_cpu_checkbox"))

        installer.chatterbox_cpu_checkbox.setChecked(True)
        self.assertTrue(installer.chatterbox_checkbox.isChecked())
        self.assertTrue(installer.chatterbox_cpu_checkbox.isChecked())
        selection = installer.snapshot_install_selection()
        self.assertFalse(selection.chatterbox)
        self.assertTrue(selection.chatterbox_cpu)

        installer.chatterbox_checkbox.setChecked(False)
        self.assertFalse(installer.chatterbox_checkbox.isChecked())
        self.assertFalse(installer.chatterbox_cpu_checkbox.isChecked())

    def test_kobold_qwen_cpu_option_maps_to_cpu_install_variant(self):
        installer = PandratorInstaller(headless=True)
        self.assertTrue(hasattr(installer, "kobold_qwen_checkbox"))
        self.assertTrue(hasattr(installer, "kobold_qwen_cpu_checkbox"))

        installer.kobold_qwen_cpu_checkbox.setChecked(True)
        self.assertTrue(installer.kobold_qwen_checkbox.isChecked())
        self.assertTrue(installer.kobold_qwen_cpu_checkbox.isChecked())
        selection = installer.snapshot_install_selection()
        self.assertFalse(selection.kobold_qwen)
        self.assertTrue(selection.kobold_qwen_cpu)
        self.assertEqual(selection.kobold_qwen_model_size, "0.6b")
        self.assertEqual(selection.kobold_qwen_quantization, "f16")

        installer.launch_kobold_qwen_checkbox.setChecked(True)
        installer.kobold_qwen_cpu_launch_checkbox.setChecked(True)
        launch_selection = installer.snapshot_launch_selection()
        self.assertTrue(launch_selection.kobold_qwen)
        self.assertTrue(launch_selection.kobold_qwen_cpu)

    def test_password_policy_tracks_local_and_lan_exposure(self):
        installer = PandratorInstaller(headless=True)
        local_index = installer.pandrator_password_scope_combo.findData("local")
        installer.pandrator_password_scope_combo.setCurrentIndex(local_index)
        self.assertEqual(installer.snapshot_launch_selection().pandrator_password_scope, "local")

        installer.pandrator_network_checkbox.setChecked(True)
        lan_selection = installer.snapshot_launch_selection()
        self.assertTrue(lan_selection.pandrator_network_access)
        self.assertEqual(lan_selection.pandrator_password_scope, "all")

        installer.pandrator_network_checkbox.setChecked(False)
        self.assertEqual(installer.snapshot_launch_selection().pandrator_password_scope, "local")

    def test_owner_password_dialog_validates_confirmation(self):
        dialog = OwnerPasswordDialog()
        dialog.password_edit.setText("a-secure-password")
        dialog.confirmation_edit.setText("different-password")
        dialog.accept_password()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertIn("do not match", dialog.error_label.text())

        dialog.confirmation_edit.setText("a-secure-password")
        dialog.accept_password()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)

    def test_launch_preferences_never_persist_plaintext_password(self):
        with tempfile.TemporaryDirectory() as workspace:
            installer = PandratorInstaller(headless=True, working_dir=workspace)
            installer.pandrator_network_checkbox.setChecked(True)
            installer.pandrator_owner_password = "a-secure-password"
            selection = installer.snapshot_launch_selection()
            installer.persist_launch_preferences(selection)
            config_path = os.path.join(workspace, "Pandrator", "config.json")
            with open(config_path, encoding="utf-8") as config_file:
                config = json.load(config_file)

        self.assertEqual(config["pandrator_password_scope"], "remote")
        self.assertTrue(config["pandrator_network_access"])
        self.assertNotIn("pandrator_owner_password", config)
        self.assertNotIn("a-secure-password", json.dumps(config))

    def test_qwen_dialog_offers_both_models_and_forces_1_7b(self):
        installer = PandratorInstaller(headless=True)
        dialog = QwenConfigDialog(installer)

        both_index = dialog.initial_model_combo.findData("both")
        self.assertGreaterEqual(both_index, 0)
        dialog.initial_model_combo.setCurrentIndex(both_index)

        self.assertEqual(dialog.get_selected_initial_model(), "both")
        self.assertEqual(dialog.get_selected_model_size(), "1.7b")
        self.assertFalse(dialog.model_size_combo.isEnabled())
        self.assertEqual(dialog.get_selected_quantization(), "f16")

    def test_qwen_snapshot_keeps_both_models_on_1_7b_if_state_is_stale(self):
        installer = PandratorInstaller(headless=True)
        installer.kobold_qwen_checkbox.setChecked(True)
        installer.kobold_qwen_initial_model = "both"
        installer.kobold_qwen_model_size = "0.6b"

        selection = installer.snapshot_install_selection()

        self.assertEqual(selection.kobold_qwen_initial_model, "both")
        self.assertEqual(selection.kobold_qwen_model_size, "1.7b")

    def test_rvc_cpu_option_maps_to_cpu_install_and_launch_variants(self):
        installer = PandratorInstaller(headless=True)

        installer.rvc_cpu_checkbox.setChecked(True)
        install_selection = installer.snapshot_install_selection()
        self.assertFalse(install_selection.rvc)
        self.assertTrue(install_selection.rvc_cpu)

        installer.launch_rvc_checkbox.setChecked(True)
        installer.rvc_cpu_launch_checkbox.setChecked(True)
        launch_selection = installer.snapshot_launch_selection()
        self.assertTrue(launch_selection.rvc)
        self.assertTrue(launch_selection.rvc_cpu)

    def test_installer_uses_compact_service_controls(self):
        installer = PandratorInstaller(headless=True)

        self.assertIsInstance(installer.xtts_checkbox, ToggleSwitch)
        self.assertIsInstance(installer.launch_xtts_checkbox, ToggleSwitch)
        self.assertEqual(installer.github_button.text(), "See on GitHub")
        self.assertFalse(installer.github_button.icon().isNull())
        self.assertFalse(hasattr(installer, "open_log_button"))
        self.assertEqual(
            installer.status_label.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Ignored,
        )
        capability_labels = installer.install_tab.findChildren(
            QLabel,
            "voiceCapabilityBadge",
        )
        self.assertEqual(len(capability_labels), 18)
        self.assertEqual(
            {label.text() for label in capability_labels},
            {"Pre-built voices", "Voice cloning"},
        )
        self.assertEqual(
            sum(label.text() == "Pre-built voices" for label in capability_labels),
            9,
        )
        self.assertEqual(
            sum(label.text() == "Voice cloning" for label in capability_labels),
            9,
        )
        self.assertTrue(
            all(label.property("supported") is not None for label in capability_labels)
        )
        self.assertEqual(
            {card.height() for card in installer.tts_engine_cards},
            {installer.tts_engine_cards[0].COLLAPSED_HEIGHT},
        )
        for control in installer.install_tab.findChildren(QCheckBox):
            control.setChecked(False)
        self.assertFalse(installer.install_button.isEnabled())
        self.assertEqual(installer.install_button.objectName(), "installButton")
        self.assertTrue(
            any(
                scroll.horizontalScrollBarPolicy()
                == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
                for scroll in installer.install_tab.findChildren(QScrollArea)
            )
        )

    @patch("pandrator_installer_launcher.PandratorInstaller.load_install_config")
    @patch("pandrator_installer_launcher.PandratorInstaller.execute_concurrently")
    @patch("pandrator_installer_launcher.PandratorInstaller.check_pixi")
    @patch("pandrator_installer_launcher.PandratorInstaller.get_pixi_executable")
    @patch("pandrator_installer_launcher.PandratorInstaller.create_pixi_env")
    @patch("pandrator_installer_launcher.PandratorInstaller.add_pixi_conda_package")
    @patch("pandrator_installer_launcher.PandratorInstaller.run_pixi_command")
    @patch("pandrator_installer_launcher.PandratorInstaller.ensure_pandrator_runtime")
    @patch("pandrator_installer_launcher.PandratorInstaller.ensure_nemo_text_processing_runtime")
    @patch("pandrator_installer_launcher.PandratorInstaller.ensure_wtpsplit_runtime")
    @patch("pandrator_installer_launcher.PandratorInstaller.ensure_pdf_ocr_runtime")
    @patch("pandrator_installer_launcher.PandratorInstaller.should_install_requirements")
    @patch("pandrator_installer_launcher.PandratorInstaller.is_chatterbox_runtime_ready")
    @patch("pandrator_installer_launcher.PandratorInstaller.install_chatterbox_api_server")
    @patch("pandrator_installer_launcher.PandratorInstaller.write_packaging_layout")
    @patch("pandrator_installer_launcher.PandratorInstaller.component_needs_package_sync")
    def test_chatterbox_update_process_flow(
        self,
        mock_comp_sync,
        mock_write_layout,
        mock_install_chatterbox,
        mock_runtime_ready,
        mock_should_install_reqs,
        mock_ensure_pdf_ocr,
        mock_ensure_wtpsplit,
        mock_ensure_nemo,
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
        # Runtime is NOT ready, so bootstrapping should trigger
        mock_runtime_ready.return_value = False

        # Run update process
        with patch.object(installer, "pull_repo"), \
             patch.object(installer, "clone_repo"), \
             patch.object(installer, "backup_state_database") as mock_backup, \
             patch("os.path.exists", return_value=True):
            
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
            self.assertFalse(any("Subdub" in task_name for task_name in tasks))

            # Verify that bootstrapping was called because runtime is not ready
            mock_install_chatterbox.assert_called_once()
            called_args, called_kwargs = mock_install_chatterbox.call_args
            # Since GPU support is True, use_cpu should be False
            self.assertFalse(called_kwargs["use_cpu"])


if __name__ == "__main__":
    unittest.main()
