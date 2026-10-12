"""Owned read-ahead lifecycle for Kobold Qwen batch audio streams."""

import ctypes
import logging
import math
import socket
import struct
import sys
from collections.abc import Callable, Iterator
from ctypes import wintypes
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from typing import Any, Protocol, cast

import requests

from .kobold_qwen_contracts import KoboldQwenBatchAudioEvent, KoboldQwenBatchMessage

logger = logging.getLogger(__name__)
_IS_WINDOWS = sys.platform == "win32"
_CANCEL_IO_EX_ERROR_NOT_FOUND = 1168
_WindowsCancelIoEx = Callable[
    [wintypes.HANDLE, ctypes.c_void_p | None], wintypes.BOOL
]
_windows_cancel_io_ex: _WindowsCancelIoEx | None = None


class _WindowsSocketOwner(Protocol):
    def gettimeout(self) -> float | None: ...

    def getsockopt(self, level: int, option: int, length: int) -> bytes: ...

    def setsockopt(self, level: int, option: int, value: bytes) -> None: ...

    def settimeout(self, value: float | None) -> None: ...

    def fileno(self) -> int: ...

    # Keep socket.makefile's keyword name in this structural interface.
    def makefile(self, mode: str, buffering: int) -> Any: ...  # noqa: F841


def _prepare_windows_receive_timeout(
    response: requests.Response,
) -> Callable[[], None] | None:
    """Use Winsock's receive timeout for the owned Windows response socket."""
    if not _IS_WINDOWS:
        return None

    shutdown = getattr(response.raw, "_sock_shutdown", None)
    owner = getattr(shutdown, "__self__", None)
    if owner is None:
        return None

    get_timeout = getattr(owner, "gettimeout", None)
    get_socket_option = getattr(owner, "getsockopt", None)
    set_socket_option = getattr(owner, "setsockopt", None)
    set_timeout = getattr(owner, "settimeout", None)
    get_fileno = getattr(owner, "fileno", None)
    makefile = getattr(owner, "makefile", None)
    if not all(
        callable(method)
        for method in (
            get_timeout,
            get_socket_option,
            set_socket_option,
            set_timeout,
            get_fileno,
            makefile,
        )
    ):
        return None
    owner = cast(_WindowsSocketOwner, owner)

    original_timeout = owner.gettimeout()
    if original_timeout is None or original_timeout <= 0:
        return None

    original_socket_timeout = owner.getsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, 4)
    if not isinstance(original_socket_timeout, bytes) or len(original_socket_timeout) != 4:
        return None

    receive_timeout = struct.pack("I", math.ceil(original_timeout * 1000))
    lease = owner.makefile("rb", buffering=0)

    def restore() -> None:
        try:
            if owner.fileno() >= 0:
                try:
                    owner.settimeout(original_timeout)
                finally:
                    owner.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_RCVTIMEO,
                        original_socket_timeout,
                    )
        finally:
            lease.close()

    try:
        owner.setsockopt(socket.SOL_SOCKET, socket.SO_RCVTIMEO, receive_timeout)
        owner.settimeout(None)
    except BaseException as setup_error:
        try:
            restore()
        except BaseException as restore_error:
            raise restore_error from setup_error
        raise
    return restore


def _get_windows_cancel_io_ex() -> _WindowsCancelIoEx:
    """Load and cache the typed Windows cancellation API on first use."""
    global _windows_cancel_io_ex
    if _windows_cancel_io_ex is None:
        load_library = getattr(ctypes, "WinDLL", None)
        if not callable(load_library):
            raise OSError("CancelIoEx is unavailable on this platform")
        kernel32 = cast(Any, load_library("kernel32", use_last_error=True))
        cancel_io_ex = kernel32.CancelIoEx
        cancel_io_ex.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        cancel_io_ex.restype = wintypes.BOOL
        _windows_cancel_io_ex = cast(_WindowsCancelIoEx, cancel_io_ex)
    return _windows_cancel_io_ex


def _cancel_windows_response_io(response: requests.Response) -> bool | None:
    """Cancel pending I/O on an admitted response's owned socket, if available."""
    if not _IS_WINDOWS:
        return None

    shutdown = getattr(response.raw, "_sock_shutdown", None)
    owner = getattr(shutdown, "__self__", None)
    get_fileno = getattr(owner, "fileno", None)
    if not callable(get_fileno):
        return None

    socket_handle = cast(int, get_fileno())
    if socket_handle <= 0:
        return None

    cancel_io_ex = _get_windows_cancel_io_ex()
    if cancel_io_ex(wintypes.HANDLE(socket_handle), None):
        return True

    get_last_error = getattr(ctypes, "get_last_error", None)
    if not callable(get_last_error):
        raise OSError("CancelIoEx failed and GetLastError is unavailable")
    error = get_last_error()
    if error == _CANCEL_IO_EX_ERROR_NOT_FOUND:
        return False
    raise OSError(error, f"CancelIoEx failed with Windows error {error}")


def iter_read_ahead_batch(
    items_count: int,
    *,
    producer: Callable[
        [Event, Callable[[requests.Response | None], None]],
        Iterator[KoboldQwenBatchAudioEvent],
    ],
    cancel_event: Event | None = None,
    thread_factory: Callable[..., Thread] = Thread,
) -> Iterator[KoboldQwenBatchAudioEvent]:
    """Read ahead while retaining ownership until the reader has stopped.

    Prefer full-duplex owner-socket shutdown for an admitted response, retaining
    public raw shutdown as a fallback. On Windows, translate its receive timeout
    and retain an unread SocketIO lease; CancelIoEx borrows that lease-pinned
    socket handle only while response_lock protects the active response. Before
    response admission, or when neither shutdown path is available, cleanup waits
    for the existing transport timeout. Response close remains the producer
    reader's responsibility.
    """
    if items_count <= 0:
        return

    messages: Queue[KoboldQwenBatchMessage] = Queue(maxsize=items_count + 2)
    stop_event = Event()
    response_lock = Lock()
    active_response: requests.Response | None = None
    active_response_restore: Callable[[], None] | None = None

    def interrupt(response: requests.Response) -> None:
        raw = response.raw
        shutdown = getattr(raw, "_sock_shutdown", None)
        args = (socket.SHUT_RDWR,)
        if not callable(shutdown):
            shutdown = getattr(raw, "shutdown", None)
            args = ()
        if callable(shutdown):
            try:
                shutdown(*args)
            except (OSError, ValueError, RuntimeError):
                logger.debug("Qwen batch response shutdown raced with cleanup.", exc_info=True)

    def set_response(response: requests.Response | None) -> None:
        nonlocal active_response, active_response_restore
        if response is None:
            with response_lock:
                restore = active_response_restore
                active_response_restore = None
                try:
                    if restore is not None:
                        restore()
                finally:
                    active_response = None
            return
        with response_lock:
            active_response = response
            active_response_restore = _prepare_windows_receive_timeout(response)
        if stop_event.is_set():
            interrupt(response)

    def interrupt_active_response() -> None:
        with response_lock:
            if active_response is not None:
                # Keep the producer from releasing the response back to its pool
                # until owner-socket shutdown finishes. The restore closure owns
                # an unread SocketIO lease; while this lock is held, that lease
                # pins the borrowed native handle through CancelIoEx.
                interrupt(active_response)
                if active_response_restore is not None:
                    _cancel_windows_response_io(active_response)

    def put_message(message: KoboldQwenBatchMessage) -> bool:
        while not stop_event.is_set():
            try:
                messages.put(message, timeout=0.05)
                return True
            except Full:
                continue
        return False

    def read() -> None:
        iterator: Iterator[KoboldQwenBatchAudioEvent] | None = None
        try:
            iterator = producer(stop_event, set_response)
            try:
                for event in iterator:
                    if not put_message(("event", event)):
                        break
            finally:
                producer_error = sys.exception()
                close = getattr(iterator, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        if producer_error is None:
                            raise
                        logger.debug("Qwen batch producer cleanup failed.", exc_info=True)
        except Exception as error:
            if not stop_event.is_set():
                put_message(("error", error))
        finally:
            if not stop_event.is_set():
                put_message(("done", None))

    reader = thread_factory(target=read, name="qwen-batch-reader", daemon=True)
    started = False
    try:
        reader.start()
        started = True
        while True:
            try:
                message = messages.get(timeout=0.05)
            except Empty:
                if cancel_event is not None and cancel_event.is_set():
                    return
                continue
            if message[0] == "error":
                raise message[1]
            if message[0] == "done":
                return
            yield message[1]
            if cancel_event is not None and cancel_event.is_set():
                return
    finally:
        primary_error = sys.exception()
        shutdown_error: BaseException | None = None
        stop_event.set()
        if started:
            while True:
                try:
                    interrupt_active_response()
                except BaseException as error:
                    if shutdown_error is None:
                        shutdown_error = error
                reader.join(timeout=0.05)
                if not reader.is_alive():
                    break
        if shutdown_error is not None and primary_error is None:
            raise shutdown_error
