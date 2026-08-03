import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dbus_next import Variant

from pandrator_manager.desktop import (
    host_process_environment,
    open_desktop_url,
)
from pandrator_manager.tray.menu import (
    EngineMenuItem,
    EngineMenuSnapshot,
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

        self.assertEqual(
            host.resolve(strict=False),
            Path(cleaned["LD_LIBRARY_PATH"]).resolve(strict=False),
        )
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
        pixmaps = notifier.IconPixmap
        self.assertEqual([32, 64], [entry[0] for entry in pixmaps])
        self.assertEqual(
            [32 * 32 * 4, 64 * 64 * 4],
            [len(entry[2]) for entry in pixmaps],
        )
        self.assertTrue(
            all(entry[2][0] == 0 for entry in pixmaps),
            "the packaged Pandrator mark should retain transparent corners",
        )

    def test_dbus_menu_preserves_all_existing_tray_actions(self):
        menu = DBusMenu(self.dispatcher)
        layout = menu._layout(0, -1, [])
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
                "Speech engines",
                "Quit tray",
            ],
            labels,
        )

    def test_dbus_menu_exposes_engine_state_and_runtime_action(self):
        snapshot = EngineMenuSnapshot(
            items=(
                EngineMenuItem(
                    component_id="kokoro",
                    service_id="tts.kokoro",
                    label="Kokoro",
                    state="running",
                    state_label="Running",
                    action="stop",
                    action_label="Stop Kokoro",
                    enabled=True,
                    is_running=True,
                ),
            )
        )
        menu = DBusMenu(self.dispatcher, snapshot)

        engine_layout = menu._layout(7, -1, [])
        parent = engine_layout[2][0].value
        action = parent[2][0].value

        self.assertEqual("Kokoro — Running", parent[1]["label"].value)
        self.assertEqual("Stop Kokoro", action[1]["label"].value)
        with mock.patch.object(self.dispatcher, "dispatch") as dispatch:
            menu.Event(101, "clicked", Variant("s", ""), 0)
        dispatch.assert_called_once_with(("tts.kokoro", "stop"))

    def test_dbus_menu_updates_revision_only_when_engine_state_changes(self):
        menu = DBusMenu(self.dispatcher)
        initial = menu.revision

        self.assertFalse(
            menu.update_engine_snapshot(
                EngineMenuSnapshot(),
                emit=False,
            )
        )
        self.assertEqual(initial, menu.revision)

        changed = EngineMenuSnapshot(available=False, message="Unavailable")
        self.assertTrue(
            menu.update_engine_snapshot(changed, emit=False)
        )
        self.assertEqual(initial + 1, menu.revision)
        self.assertEqual(
            "Unavailable",
            menu._layout(7, -1, [])[2][0].value[1]["label"].value,
        )

    def test_status_notifier_tooltip_reports_engine_summary(self):
        notifier = StatusNotifierItem(self.dispatcher)
        notifier.update_engine_snapshot(
            EngineMenuSnapshot(
                items=(
                    EngineMenuItem(
                        component_id="kokoro",
                        service_id="tts.kokoro",
                        label="Kokoro",
                        state="stopped",
                        state_label="Stopped",
                        action="start",
                        action_label="Start Kokoro",
                        enabled=True,
                        is_running=False,
                    ),
                )
            ),
            emit=False,
        )

        self.assertEqual("0/1 engine running", notifier.ToolTip[3])

    def test_disabled_engine_action_cannot_be_dispatched(self):
        snapshot = EngineMenuSnapshot(
            items=(
                EngineMenuItem(
                    component_id="kokoro",
                    service_id="tts.kokoro",
                    label="Kokoro",
                    state="stopped",
                    state_label="Stopped",
                    action="start",
                    action_label="Start Kokoro",
                    enabled=False,
                    is_running=False,
                ),
            ),
            busy=True,
        )
        menu = DBusMenu(self.dispatcher, snapshot)

        with mock.patch.object(self.dispatcher, "dispatch") as dispatch:
            menu.Event(101, "clicked", Variant("s", ""), 0)

        dispatch.assert_not_called()


class StatusNotifierActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_action_is_forwarded_off_the_dbus_event_loop(self):
        quit_event = asyncio.Event()
        engine_action = mock.Mock()
        dispatcher = _ActionDispatcher(
            {},
            quit_event,
            engine_action=engine_action,
        )

        await dispatcher._invoke(("tts.kokoro", "stop"))

        engine_action.assert_called_once_with("tts.kokoro", "stop")


if __name__ == "__main__":
    unittest.main()
