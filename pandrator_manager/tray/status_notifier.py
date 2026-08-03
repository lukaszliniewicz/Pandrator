"""Native Linux StatusNotifierItem and DBusMenu tray implementation."""

# D-Bus signatures are deliberately expressed as string annotations for
# dbus-next; they are protocol declarations rather than Python type names.
# ruff: noqa: F722, F821

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable, Mapping

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import BusType, PropertyAccess, RequestNameReply
from dbus_next.service import ServiceInterface, dbus_property, method, signal
from PIL import Image

from .icon import load_tray_icon
from .menu import EngineMenuSnapshot, unavailable_engine_snapshot


class StatusNotifierUnavailable(RuntimeError):
    """The desktop does not provide a usable StatusNotifier host."""


def _argb_bytes(image: Image.Image) -> bytes:
    rgba = image.tobytes()
    argb = bytearray(len(rgba))
    for offset in range(0, len(rgba), 4):
        red, green, blue, alpha = rgba[offset : offset + 4]
        argb[offset : offset + 4] = bytes((alpha, red, green, blue))
    return bytes(argb)


def _icon_pixmaps():
    logo = load_tray_icon()
    pixmaps = []
    for size in (32, 64):
        image = logo.resize((size, size), Image.Resampling.LANCZOS)
        pixmaps.append([size, size, _argb_bytes(image)])
    return pixmaps


class _ActionDispatcher:
    def __init__(
        self,
        actions: Mapping[str, Callable[[], None]],
        quit_event: asyncio.Event,
        engine_action: Callable[[str, str], None] | None = None,
    ):
        self.actions = dict(actions)
        self.quit_event = quit_event
        self.engine_action = engine_action

    async def _invoke(self, action: str | tuple[str, str]) -> None:
        try:
            if isinstance(action, tuple):
                if self.engine_action is not None:
                    await asyncio.to_thread(
                        self.engine_action,
                        action[0],
                        action[1],
                    )
            else:
                await asyncio.to_thread(self.actions[action])
        except Exception:
            logging.exception("Desktop tray action %s failed.", action)

    def dispatch(self, action: str | tuple[str, str]) -> None:
        if action == "quit":
            self.quit_event.set()
            return
        if (
            isinstance(action, tuple)
            and self.engine_action is not None
        ) or action in self.actions:
            asyncio.create_task(self._invoke(action))


class StatusNotifierItem(ServiceInterface):
    def __init__(self, dispatcher: _ActionDispatcher):
        super().__init__("org.kde.StatusNotifierItem")
        self.dispatcher = dispatcher
        self.pixmaps = _icon_pixmaps()
        self.tooltip_description = (
            "Open Pandrator or installation and recovery."
        )

    def update_engine_snapshot(
        self,
        snapshot: EngineMenuSnapshot,
        *,
        emit: bool = True,
    ) -> None:
        description = snapshot.summary
        if description == self.tooltip_description:
            return
        self.tooltip_description = description
        if emit:
            self.NewToolTip()

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
            self.tooltip_description,
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
    _BASE_ITEMS = {
        0: {},
        1: {"label": "Open Pandrator"},
        2: {"label": "Open setup / recovery"},
        3: {"type": "separator"},
        4: {"label": "Start Pandrator"},
        5: {"label": "Stop Pandrator"},
        6: {"type": "separator"},
        7: {
            "label": "Speech engines",
            "children-display": "submenu",
        },
        8: {"type": "separator"},
        9: {"label": "Quit tray"},
    }
    _BASE_ACTIONS = {
        1: "open_pandrator",
        2: "open_recovery",
        4: "start_pandrator",
        5: "stop_pandrator",
        9: "quit",
    }

    def __init__(
        self,
        dispatcher: _ActionDispatcher,
        snapshot: EngineMenuSnapshot | None = None,
    ):
        super().__init__("com.canonical.dbusmenu")
        self.dispatcher = dispatcher
        self.revision = 1
        self._snapshot = snapshot or EngineMenuSnapshot()
        self._items: dict[int, dict[str, str | bool]] = {}
        self._actions: dict[int, str | tuple[str, str]] = {}
        self._children: dict[int, list[int]] = {}
        self._rebuild()

    def _rebuild(self) -> None:
        self._items = {
            item_id: dict(properties)
            for item_id, properties in self._BASE_ITEMS.items()
        }
        self._actions = dict(self._BASE_ACTIONS)
        self._children = {
            0: list(range(1, 10)),
            7: [],
        }

        if not self._snapshot.available:
            self._items[100] = {
                "label": (
                    self._snapshot.message
                    or "Engine status unavailable"
                ),
                "enabled": False,
            }
            self._children[7].append(100)
            return
        if not self._snapshot.items:
            self._items[100] = {
                "label": "No optional engines installed",
                "enabled": False,
            }
            self._children[7].append(100)
            return

        next_id = 100
        for engine in self._snapshot.items:
            parent_id = next_id
            action_id = next_id + 1
            next_id += 2
            self._items[parent_id] = {
                "label": f"{engine.label} — {engine.state_label}",
                "children-display": "submenu",
            }
            self._children[7].append(parent_id)
            self._children[parent_id] = [action_id]
            self._items[action_id] = {
                "label": engine.action_label,
                "enabled": engine.enabled,
            }
            if engine.action is not None:
                self._actions[action_id] = (
                    engine.service_id,
                    engine.action,
                )

    def update_engine_snapshot(
        self,
        snapshot: EngineMenuSnapshot,
        *,
        emit: bool = True,
    ) -> bool:
        if snapshot == self._snapshot:
            return False
        self._snapshot = snapshot
        self._rebuild()
        self.revision += 1
        if emit:
            self.LayoutUpdated(self.revision, 0)
        return True

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

    def _properties(self, item_id: int, names: list[str]):
        raw = self._items.get(item_id, {})
        selected = (
            raw
            if not names
            else {
                key: value
                for key, value in raw.items()
                if key in names
            }
        )
        properties = {}
        for key, value in selected.items():
            signature = "b" if isinstance(value, bool) else "s"
            properties[key] = Variant(signature, value)
        if (
            item_id not in {0, 3, 6, 8}
            and "enabled" not in raw
            and (not names or "enabled" in names)
        ):
            properties["enabled"] = Variant("b", True)
        return properties

    def _layout(self, item_id: int, depth: int, names: list[str]):
        children = []
        if item_id in self._children and depth != 0:
            child_depth = depth - 1 if depth > 0 else depth
            children = [
                Variant(
                    "(ia{sv}av)",
                    self._layout(child_id, child_depth, names),
                )
                for child_id in self._children[item_id]
            ]
        return [item_id, self._properties(item_id, names), children]

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
        selected = item_ids or list(self._items)
        return [
            [item_id, self._properties(item_id, property_names)]
            for item_id in selected
            if item_id in self._items
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
        enabled = self._items.get(item_id, {}).get("enabled", True)
        if event_id == "clicked" and item_id in self._actions and enabled:
            self.dispatcher.dispatch(self._actions[item_id])

    @method()
    def EventGroup(self, events: "a(isvu)") -> "ai":
        invalid = []
        for item_id, event_id, _data, _timestamp in events:
            action = self._actions.get(item_id)
            enabled = self._items.get(item_id, {}).get(
                "enabled",
                True,
            )
            if action is None or not enabled:
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


async def _poll_engine_status(
    provider: Callable[[], EngineMenuSnapshot],
    menu: DBusMenu,
    notifier: StatusNotifierItem,
    quit_event: asyncio.Event,
) -> None:
    while not quit_event.is_set():
        try:
            snapshot = await asyncio.to_thread(provider)
            if not isinstance(snapshot, EngineMenuSnapshot):
                raise TypeError("Engine provider returned an invalid snapshot.")
        except Exception:
            logging.exception("Could not refresh native tray engine status.")
            snapshot = unavailable_engine_snapshot()
        menu.update_engine_snapshot(snapshot)
        notifier.update_engine_snapshot(snapshot)
        try:
            await asyncio.wait_for(quit_event.wait(), timeout=2.5)
        except TimeoutError:
            pass


async def _serve(
    actions: Mapping[str, Callable[[], None]],
    *,
    engine_provider: Callable[[], EngineMenuSnapshot] | None = None,
    engine_action: Callable[[str, str], None] | None = None,
) -> None:
    try:
        bus = await MessageBus(bus_type=BusType.SESSION).connect()
    except Exception as error:
        raise StatusNotifierUnavailable(
            f"The desktop session bus is unavailable: {error}"
        ) from error
    quit_event = asyncio.Event()
    dispatcher = _ActionDispatcher(
        actions,
        quit_event,
        engine_action=engine_action,
    )
    notifier = StatusNotifierItem(dispatcher)
    initial_snapshot = (
        unavailable_engine_snapshot("Loading engine status")
        if engine_provider is not None
        else EngineMenuSnapshot()
    )
    menu = DBusMenu(dispatcher, initial_snapshot)
    notifier.update_engine_snapshot(initial_snapshot, emit=False)
    service_name = f"org.freedesktop.StatusNotifierItem-{os.getpid()}-1"
    poll_task = None
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
        if engine_provider is not None:
            poll_task = asyncio.create_task(
                _poll_engine_status(
                    engine_provider,
                    menu,
                    notifier,
                    quit_event,
                )
            )
        await quit_event.wait()
    except StatusNotifierUnavailable:
        raise
    except Exception as error:
        raise StatusNotifierUnavailable(
            f"The desktop StatusNotifier host is unavailable: {error}"
        ) from error
    finally:
        if poll_task is not None:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task
        bus.disconnect()


def run_status_notifier(
    actions: Mapping[str, Callable[[], None]],
    *,
    engine_provider: Callable[[], EngineMenuSnapshot] | None = None,
    engine_action: Callable[[str, str], None] | None = None,
) -> None:
    asyncio.run(
        _serve(
            actions,
            engine_provider=engine_provider,
            engine_action=engine_action,
        )
    )
