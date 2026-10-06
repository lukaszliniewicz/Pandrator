"""The native tray keeps its exported D-Bus schema and marshalling contract."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.etree.ElementTree import tostring

import pytest

from pandrator_manager.tray import wire_types
from pandrator_manager.tray.status_notifier import DBusMenu, StatusNotifierItem, _ActionDispatcher


def test_exported_schema_matches_the_retained_native_protocol():
    dispatcher = _ActionDispatcher({}, asyncio.Event())
    objects = [StatusNotifierItem(dispatcher), DBusMenu(dispatcher)]
    actual = {o.name: tostring(o.introspect().to_xml(), encoding="unicode") for o in objects}
    expected = json.loads((Path(__file__).parent / "fixtures/tray_introspection.json").read_text())
    assert actual == expected


def test_service_annotations_keep_runtime_wire_strings():
    assert wire_types.DBusString == "s"
    assert wire_types.DBusInt32 == "i"
    assert wire_types.DBusUInt32 == "u"
    assert wire_types.DBusStringArray == "as"
    assert wire_types.DBusMenuLayout == "u(ia{sv}av)"
    assert StatusNotifierItem.Category.fget.__annotations__["return"] == "s"
    assert DBusMenu.GetLayout.__annotations__["return"] == "u(ia{sv}av)"


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("dbus-run-session") is None,
    reason="A disposable dbus-run-session is required",
)
def test_private_bus_roundtrip_marshals_tray_properties_menu_and_signals():
    script = r"""
import asyncio
from dbus_next.aio import MessageBus
from dbus_next.signature import Variant
from pandrator_manager.tray.status_notifier import DBusMenu, StatusNotifierItem, _ActionDispatcher

async def run():
    server = await MessageBus().connect()
    client = await MessageBus().connect()
    dispatcher = _ActionDispatcher({}, asyncio.Event())
    notifier = StatusNotifierItem(dispatcher)
    menu = DBusMenu(dispatcher)
    try:
        server.export('/StatusNotifierItem', notifier)
        server.export('/MenuBar', menu)
        await server.request_name('org.pandrator.TestTray')
        item_xml = await client.introspect('org.pandrator.TestTray', '/StatusNotifierItem')
        item = client.get_proxy_object('org.pandrator.TestTray', '/StatusNotifierItem', item_xml)
        properties = item.get_interface('org.freedesktop.DBus.Properties')
        category = await properties.call_get('org.kde.StatusNotifierItem', 'Category')
        assert isinstance(category, Variant) and category.value == 'ApplicationStatus'
        pixmaps = await properties.call_get('org.kde.StatusNotifierItem', 'IconPixmap')
        assert pixmaps.signature == 'a(iiay)' and [p[0] for p in pixmaps.value] == [32, 64]
        tooltip = await properties.call_get('org.kde.StatusNotifierItem', 'ToolTip')
        assert tooltip.signature == '(sa(iiay)ss)' and tooltip.value[2] == 'Pandrator Manager'
        menu_xml = await client.introspect('org.pandrator.TestTray', '/MenuBar')
        proxy = client.get_proxy_object('org.pandrator.TestTray', '/MenuBar', menu_xml)
        api = proxy.get_interface('com.canonical.dbusmenu')
        revision, layout = await api.call_get_layout(0, -1, [])
        assert revision == 1 and layout[0] == 0 and layout[2]
        groups = await api.call_get_group_properties([1, 10], ['label'])
        assert [g[0] for g in groups] == [1, 10]
        assert groups[0][1]['label'].value == 'Open Pandrator'
        assert await api.call_about_to_show(0) is False
        assert await api.call_about_to_show_group([0]) == [[], []]
        assert await api.call_event_group([[999, 'clicked', Variant('s', ''), 0]]) == [999]
        await api.call_event(10, 'clicked', Variant('s', ''), 0)
        assert dispatcher.quit_event.is_set()
        received = asyncio.Event()
        api.on_layout_updated(lambda revision, parent: received.set())
        menu.LayoutUpdated(2, 0)
        await asyncio.wait_for(received.wait(), timeout=2)
    finally:
        client.disconnect()
        server.disconnect()

asyncio.run(asyncio.wait_for(run(), timeout=5))
"""
    result = subprocess.run(
        ["dbus-run-session", "--", sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend_client_import_does_not_load_interactive_launcher():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import pandrator_manager.client; "
            "assert 'pandrator_manager.launcher' not in sys.modules",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
