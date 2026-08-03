import unittest
from unittest import mock

from pandrator_manager.tray import TrayApplication
from pandrator_manager.tray.menu import build_engine_menu_snapshot


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items):
        self._items = items

    @property
    def items(self):
        if len(self._items) == 1 and callable(self._items[0]):
            return tuple(self._items[0]())
        return self._items


class FakeMenuItem:
    def __init__(self, text, action=None, **options):
        self._text = text
        self.action = action
        self.submenu = action if isinstance(action, FakeMenu) else None
        self.options = options

    @property
    def text(self):
        if callable(self._text):
            return self._text(None)
        return self._text


def component(
    component_id="kokoro",
    *,
    label="Kokoro",
    state="present",
    service_id="tts.kokoro",
):
    return {
        "definition": {
            "id": component_id,
            "label": label,
            "service_key": service_id,
            "supported_actions": [
                "install",
                "remove",
                "start",
                "stop",
            ],
        },
        "inspection": {"state": state},
    }


def service(
    *,
    process=None,
    desired_running=False,
    health="stopped",
):
    return {
        "id": "tts.kokoro",
        "component_id": "kokoro",
        "desired_running": desired_running,
        "process": process,
        "health": {
            "state": health,
            "service_id": "tts.kokoro",
        },
    }


class EngineMenuSnapshotTests(unittest.TestCase):
    def test_running_engine_is_counted_and_can_be_stopped(self):
        snapshot = build_engine_menu_snapshot(
            [component()],
            [
                service(
                    process={"pid": 8123},
                    desired_running=True,
                    health="healthy",
                )
            ],
        )

        self.assertEqual("1/1 engine running", snapshot.summary)
        self.assertEqual("Running", snapshot.items[0].state_label)
        self.assertEqual("stop", snapshot.items[0].action)
        self.assertEqual("Stop Kokoro", snapshot.items[0].action_label)
        self.assertTrue(snapshot.items[0].enabled)

    def test_pending_restart_can_be_cancelled_from_the_tray(self):
        snapshot = build_engine_menu_snapshot(
            [component()],
            [service(desired_running=True, health="unhealthy")],
        )

        self.assertEqual("Restarting", snapshot.items[0].state_label)
        self.assertEqual("stop", snapshot.items[0].action)
        self.assertFalse(snapshot.items[0].is_running)

    def test_stopped_engine_can_be_started(self):
        snapshot = build_engine_menu_snapshot(
            [component()],
            [service()],
        )

        self.assertEqual("Stopped", snapshot.items[0].state_label)
        self.assertEqual("start", snapshot.items[0].action)
        self.assertEqual("Start Kokoro", snapshot.items[0].action_label)

    def test_failed_engine_is_not_described_as_stopped(self):
        snapshot = build_engine_menu_snapshot(
            [component()],
            [service(desired_running=True, health="failed")],
        )

        self.assertEqual("Failed", snapshot.items[0].state_label)
        self.assertEqual("stop", snapshot.items[0].action)

    def test_busy_manager_disables_runtime_actions(self):
        snapshot = build_engine_menu_snapshot(
            [component()],
            [service()],
            busy=True,
        )

        self.assertTrue(snapshot.busy)
        self.assertFalse(snapshot.items[0].enabled)
        self.assertEqual("start", snapshot.items[0].action)

    def test_only_installed_optional_engines_are_listed_and_sorted(self):
        snapshot = build_engine_menu_snapshot(
            [
                component(
                    "xtts",
                    label="XTTS v2",
                    state="absent",
                    service_id="tts.xtts",
                ),
                component(
                    "silero",
                    label="Silero",
                    service_id="tts.silero",
                ),
                component(),
            ],
            [
                service(),
                {
                    **service(),
                    "id": "tts.silero",
                    "component_id": "silero",
                },
            ],
        )

        self.assertEqual(
            ["Kokoro", "Silero"],
            [item.label for item in snapshot.items],
        )

    def test_degraded_engine_prompts_for_repair_instead_of_start(self):
        snapshot = build_engine_menu_snapshot(
            [component(state="degraded")],
            [],
        )

        self.assertEqual("Needs repair", snapshot.items[0].state_label)
        self.assertIsNone(snapshot.items[0].action)
        self.assertFalse(snapshot.items[0].enabled)


class TrayApplicationEngineTests(unittest.TestCase):
    def test_pystray_backend_uses_the_packaged_pandrator_mark(self):
        image = TrayApplication(mock.Mock())._image()

        self.assertEqual((64, 64), image.size)
        self.assertEqual(0, image.getpixel((0, 0))[3])

    def test_tray_reads_components_services_and_operation_state(self):
        client = mock.Mock()
        client.status.return_value = {"active_operation_id": None}
        client.components.return_value = [component()]
        client.services.return_value = [service()]
        application = TrayApplication(client)

        snapshot = application.engine_snapshot()

        self.assertEqual("0/1 engine running", snapshot.summary)
        self.assertEqual(
            "Pandrator Manager — 0/1 engine running",
            application._status_label(),
        )

    def test_tray_engine_action_uses_manager_runtime_api(self):
        client = mock.Mock()
        client.status.return_value = {"active_operation_id": None}
        client.components.return_value = [component()]
        client.services.return_value = [service()]
        application = TrayApplication(client)

        application.control_engine("tts.kokoro", "start")

        client.runtime.assert_called_once_with(
            "start",
            ("tts.kokoro",),
        )

    def test_pystray_menu_exposes_engine_state_and_action(self):
        client = mock.Mock()
        client.status.return_value = {"active_operation_id": None}
        client.components.return_value = [component()]
        client.services.return_value = [service()]
        application = TrayApplication(client)
        application.engine_snapshot()

        fake_pystray = mock.Mock(Menu=FakeMenu, MenuItem=FakeMenuItem)
        with mock.patch.dict("sys.modules", {"pystray": fake_pystray}):
            top_level = application._pystray_menu_items()
            engine_group = next(
                item
                for item in top_level
                if getattr(item, "text", "").startswith("Speech engines")
            )
            engine = engine_group.submenu.items[0]

        self.assertEqual("Speech engines — 0/1 running", engine_group.text)
        self.assertEqual("Kokoro — Stopped", engine.text)
        self.assertEqual("Start Kokoro", engine.submenu.items[0].text)


if __name__ == "__main__":
    unittest.main()
