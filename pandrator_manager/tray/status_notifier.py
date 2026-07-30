"""Native Linux StatusNotifierItem and DBusMenu tray implementation."""

# D-Bus signatures are deliberately expressed as string annotations for
# dbus-next; they are protocol declarations rather than Python type names.
# ruff: noqa: F722, F821

import asyncio
import logging
import os
from collections.abc import Callable, Mapping

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, PropertyAccess, RequestNameReply
from dbus_next.service import ServiceInterface, dbus_property, method, signal
from PIL import Image, ImageDraw


class StatusNotifierUnavailable(RuntimeError):
    """The desktop does not provide a usable StatusNotifier host."""


def _icon_pixmaps():
    pixmaps = []
    for size in (32, 64):
        image = Image.new("RGBA", (size, size), "#211b2b")
        draw = ImageDraw.Draw(image)
        inset = max(3, size // 9)
        radius = max(4, size // 5)
        draw.rounded_rectangle(
            (inset, inset, size - inset, size - inset),
            radius=radius,
            fill="#ad8ce8",
        )
        draw.polygon(
            (
                (size * 11 // 32, size * 9 // 32),
                (size * 24 // 32, size // 2),
                (size * 11 // 32, size * 23 // 32),
            ),
            fill="#211b2b",
        )
        rgba = image.tobytes()
        argb = bytearray(len(rgba))
        for offset in range(0, len(rgba), 4):
            red, green, blue, alpha = rgba[offset : offset + 4]
            argb[offset : offset + 4] = bytes((alpha, red, green, blue))
        pixmaps.append([size, size, bytes(argb)])
    return pixmaps


class _ActionDispatcher:
    def __init__(
        self,
        actions: Mapping[str, Callable[[], None]],
        quit_event: asyncio.Event,
    ):
        self.actions = dict(actions)
        self.quit_event = quit_event

    async def _invoke(self, action: str) -> None:
        try:
            await asyncio.to_thread(self.actions[action])
        except Exception:
            logging.exception("Desktop tray action %s failed.", action)

    def dispatch(self, action: str) -> None:
        if action == "quit":
            self.quit_event.set()
            return
        if action in self.actions:
            asyncio.create_task(self._invoke(action))


class StatusNotifierItem(ServiceInterface):
    def __init__(self, dispatcher: _ActionDispatcher):
        super().__init__("org.kde.StatusNotifierItem")
        self.dispatcher = dispatcher
        self.pixmaps = _icon_pixmaps()

    @dbus_property(access=PropertyAccess.READ)
    def Category(self) -> "s":
        return "ApplicationStatus"

    @dbus_property(access=PropertyAccess.READ)
    def Id(self) -> "s":
        return "Pandrator"

    @dbus_property(access=PropertyAccess.READ)
    def Title(self) -> "s":
        return "Pandrator Manager"

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "Active"

    @dbus_property(access=PropertyAccess.READ)
    def WindowId(self) -> "u":
        return 0

    @dbus_property(access=PropertyAccess.READ)
    def IconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def IconPixmap(self) -> "a(iiay)":
        return self.pixmaps

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def OverlayIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def AttentionIconPixmap(self) -> "a(iiay)":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def AttentionMovieName(self) -> "s":
        return ""

    @dbus_property(access=PropertyAccess.READ)
    def ToolTip(self) -> "(sa(iiay)ss)":
        return [
            "",
            self.pixmaps,
            "Pandrator Manager",
            "Open Pandrator or installation and recovery.",
        ]

    @dbus_property(access=PropertyAccess.READ)
    def ItemIsMenu(self) -> "b":
        return False

    @dbus_property(access=PropertyAccess.READ)
    def Menu(self) -> "o":
        return "/MenuBar"

    @method()
    def ContextMenu(self, _x: "i", _y: "i"):
        return None

    @method()
    def Activate(self, _x: "i", _y: "i"):
        self.dispatcher.dispatch("open_pandrator")

    @method()
    def SecondaryActivate(self, _x: "i", _y: "i"):
        self.dispatcher.dispatch("open_recovery")

    @method()
    def Scroll(self, _delta: "i", _orientation: "s"):
        return None

    @signal()
    def NewTitle(self):
        return None

    @signal()
    def NewIcon(self):
        return None

    @signal()
    def NewAttentionIcon(self):
        return None

    @signal()
    def NewOverlayIcon(self):
        return None

    @signal()
    def NewToolTip(self):
        return None

    @signal()
    def NewStatus(self, status) -> "s":
        return status


class DBusMenu(ServiceInterface):
    _ITEMS = {
        0: {},
        1: {"label": "Open Pandrator"},
        2: {"label": "Open setup / recovery"},
        3: {"type": "separator"},
        4: {"label": "Start Pandrator"},
        5: {"label": "Stop Pandrator"},
        6: {"type": "separator"},
        7: {"label": "Quit tray"},
    }
    _ACTIONS = {
        1: "open_pandrator",
        2: "open_recovery",
        4: "start_pandrator",
        5: "stop_pandrator",
        7: "quit",
    }

    def __init__(self, dispatcher: _ActionDispatcher):
        super().__init__("com.canonical.dbusmenu")
        self.dispatcher = dispatcher
        self.revision = 1

    @dbus_property(access=PropertyAccess.READ)
    def Version(self) -> "u":
        return 3

    @dbus_property(access=PropertyAccess.READ)
    def Status(self) -> "s":
        return "normal"

    @dbus_property(access=PropertyAccess.READ)
    def TextDirection(self) -> "s":
        return "ltr"

    @dbus_property(access=PropertyAccess.READ)
    def IconThemePath(self) -> "as":
        return []

    @staticmethod
    def _properties(item_id: int, names: list[str]):
        raw = DBusMenu._ITEMS.get(item_id, {})
        selected = raw if not names else {
            key: value for key, value in raw.items() if key in names
        }
        properties = {}
        for key, value in selected.items():
            properties[key] = Variant("s", value)
        if item_id not in {0, 3, 6} and (not names or "enabled" in names):
            properties["enabled"] = Variant("b", True)
        return properties

    @classmethod
    def _layout(cls, item_id: int, depth: int, names: list[str]):
        children = []
        if item_id == 0 and depth != 0:
            child_depth = depth - 1 if depth > 0 else depth
            children = [
                Variant(
                    "(ia{sv}av)",
                    cls._layout(child_id, child_depth, names),
                )
                for child_id in range(1, 8)
            ]
        return [item_id, cls._properties(item_id, names), children]

    @method()
    def GetLayout(
        self,
        parent_id: "i",
        recursion_depth: "i",
        property_names: "as",
    ) -> "u(ia{sv}av)":
        return [
            self.revision,
            self._layout(parent_id, recursion_depth, property_names),
        ]

    @method()
    def GetGroupProperties(
        self,
        item_ids: "ai",
        property_names: "as",
    ) -> "a(ia{sv})":
        selected = item_ids or list(self._ITEMS)
        return [
            [item_id, self._properties(item_id, property_names)]
            for item_id in selected
            if item_id in self._ITEMS
        ]

    @method()
    def GetProperty(self, item_id: "i", name: "s") -> "v":
        return self._properties(item_id, [name]).get(name, Variant("s", ""))

    @method()
    def Event(
        self,
        item_id: "i",
        event_id: "s",
        _data: "v",
        _timestamp: "u",
    ):
        if event_id == "clicked" and item_id in self._ACTIONS:
            self.dispatcher.dispatch(self._ACTIONS[item_id])

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        invalid = []
        for item_id, event_id, _data, _timestamp in events:
            action = self._ACTIONS.get(item_id)
            if action is None:
                invalid.append(item_id)
            elif event_id == "clicked":
                self.dispatcher.dispatch(action)
        return invalid

    @method()
    def AboutToShow(self, _item_id: "i") -> "b":
        return False

    @method()
    def AboutToShowGroup(self, _item_ids: "ai") -> "aiai":
        return [[], []]

    @signal()
    def LayoutUpdated(self, revision, parent) -> "ui":
        return [revision, parent]

    @signal()
    def ItemsPropertiesUpdated(self, updated, removed) -> "a(ia{sv})a(ias)":
        return [updated, removed]


async def _serve(actions: Mapping[str, Callable[[], None]]) -> None:
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except Exception as error:
        raise StatusNotifierUnavailable(
            f"The desktop session bus is unavailable: {error}"
        ) from error
    quit_event = asyncio.Event()
    dispatcher = _ActionDispatcher(actions, quit_event)
    notifier = StatusNotifierItem(dispatcher)
    menu = DBusMenu(dispatcher)
    service_name = f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
    try:
        bus.export("/StatusNotifierItem", notifier)
        bus.export("/MenuBar", menu)
        reply = await bus.request_name(service_name)
        if reply not in {
            RequestNameReply.PRIMARY_OWNER,
            RequestNameReply.ALREADY_OWNER,
        }:
            raise StatusNotifierUnavailable(
                "The desktop tray service name is already in use."
            )
        introspection = await bus.introspect(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
        )
        watcher = bus.get_proxy_object(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            introspection,
        ).get_interface("org.kde.StatusNotifierWatcher")
        await watcher.call_register_status_notifier_item(service_name)
        await quit_event.wait()
    except StatusNotifierUnavailable:
        raise
    except Exception as error:
        raise StatusNotifierUnavailable(
            f"The desktop StatusNotifier host is unavailable: {error}"
        ) from error
    finally:
        bus.disconnect()


def run_status_notifier(
    actions: Mapping[str, Callable[[], None]],
) -> None:
    asyncio.run(_serve(actions))
