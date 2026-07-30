import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandrator_manager.desktop import (
    host_process_environment,
    open_desktop_url,
)
from pandrator_manager.tray.status_notifier import (
    DBusMenu,
    StatusNotifierItem,
    _ActionDispatcher,
)


class HostDesktopLaunchTests(unittest.TestCase):
    def test_frozen_library_paths_are_removed_without_losing_host_paths(self):
        temporary = Path(tempfile.gettempdir())
        first = temporary / "_MEIfirst"
        second = temporary / "_MEIsecond"
        host = temporary / "host-libraries"
        environment = {
            "LD_LIBRARY_PATH": os.pathsep.join((str(first), str(second))),
            "LD_LIBRARY_PATH_ORIG": os.pathsep.join((str(first), str(host))),
            "UNCHANGED": "yes",
            "_MEIPASS2": str(first),
            "PYTHONHOME": str(first),
        }

        cleaned = host_process_environment(environment)

        self.assertEqual(str(host.resolve(strict=False)), cleaned["LD_LIBRARY_PATH"])
        self.assertNotIn("LD_LIBRARY_PATH_ORIG", cleaned)
        self.assertNotIn("_MEIPASS2", cleaned)
        self.assertNotIn("PYTHONHOME", cleaned)
        self.assertEqual("yes", cleaned["UNCHANGED"])

    def test_linux_url_open_uses_xdg_open_with_a_clean_detached_environment(self):
        process = mock.Mock(pid=4123)
        process.wait.return_value = 0
        with (
            mock.patch("pandrator_manager.desktop.sys.platform", "linux"),
            mock.patch(
                "pandrator_manager.desktop.shutil.which",
                return_value="/usr/bin/xdg-open",
            ),
            mock.patch(
                "pandrator_manager.desktop.host_process_environment",
                return_value={"SAFE": "1"},
            ),
            mock.patch(
                "pandrator_manager.desktop.subprocess.Popen",
                return_value=process,
            ) as popen,
            mock.patch("pandrator_manager.desktop.threading.Thread") as thread,
        ):
            opened = open_desktop_url("http://127.0.0.1:8098/recovery")

        self.assertTrue(opened)
        self.assertEqual(
            ["/usr/bin/xdg-open", "http://127.0.0.1:8098/recovery"],
            popen.call_args.args[0],
        )
        self.assertEqual({"SAFE": "1"}, popen.call_args.kwargs["env"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        thread.return_value.start.assert_called_once_with()

    def test_desktop_opener_rejects_non_http_urls(self):
        with mock.patch("pandrator_manager.desktop.webbrowser.open") as browser:
            self.assertFalse(open_desktop_url("file:///tmp/private"))
        browser.assert_not_called()


class StatusNotifierContractTests(unittest.TestCase):
    def setUp(self):
        self.quit_event = asyncio.Event()
        self.dispatcher = _ActionDispatcher({}, self.quit_event)

    def test_status_notifier_exposes_native_activation_and_pixmap_contract(self):
        notifier = StatusNotifierItem(self.dispatcher)
        interface = notifier.introspect()
        method_names = {member.name for member in interface.methods}
        property_names = {member.name for member in interface.properties}

        self.assertIn("Activate", method_names)
        self.assertIn("SecondaryActivate", method_names)
        self.assertIn("IconPixmap", property_names)
        self.assertIn("Menu", property_names)
        self.assertEqual("/MenuBar", notifier.Menu)
        self.assertEqual([32, 64], [entry[0] for entry in notifier.IconPixmap])

    def test_dbus_menu_preserves_all_existing_tray_actions(self):
        layout = DBusMenu._layout(0, -1, [])
        labels = [
            child.value[1]["label"].value
            for child in layout[2]
            if "label" in child.value[1]
        ]

        self.assertEqual(
            [
                "Open Pandrator",
                "Open setup / recovery",
                "Start Pandrator",
                "Stop Pandrator",
                "Quit tray",
            ],
            labels,
        )


if __name__ == "__main__":
    unittest.main()
