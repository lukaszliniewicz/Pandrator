"""Python types during checking and dbus-next wire signatures at runtime.

Service annotations are evaluated normally: do not postpone them in modules
using these aliases. dbus-next consumes the resulting signature strings.
Composite return values use lists as required by dbus-next's marshaller.
"""

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from dbus_next.service import signal as _signal

if TYPE_CHECKING:
    from dbus_next.signature import Variant

    DBusString = str
    DBusInt32 = int
    DBusUInt32 = int
    DBusBool = bool
    DBusObjectPath = str
    DBusVariant = Variant
    DBusStringArray = list[str]
    DBusInt32Array = list[int]
    DBusIconPixmaps = list[list[Any]]
    DBusToolTip = list[Any]
    DBusMenuLayout = list[Any]
    DBusGroupProperties = list[list[Any]]
    DBusMenuEvents = list[list[Any]]
    DBusShowGroups = list[list[int]]
    DBusLayoutUpdate = list[int]
    DBusItemsUpdate = list[Any]
else:
    DBusString = "s"
    DBusInt32 = "i"
    DBusUInt32 = "u"
    DBusBool = "b"
    DBusObjectPath = "o"
    DBusVariant = "v"
    DBusStringArray = "as"
    DBusInt32Array = "ai"
    DBusIconPixmaps = "a(iiay)"
    DBusToolTip = "(sa(iiay)ss)"
    DBusMenuLayout = "u(ia{sv}av)"
    DBusGroupProperties = "a(ia{sv})"
    DBusMenuEvents = "a(isvu)"
    DBusShowGroups = "aiai"
    DBusLayoutUpdate = "ui"
    DBusItemsUpdate = "a(ia{sv})a(ias)"

_Function = TypeVar("_Function", bound=Callable[..., Any])


def signal() -> Callable[[_Function], _Function]:
    """Retain the service method signature through dbus-next's decorator."""

    return cast(Callable[[_Function], _Function], _signal())
