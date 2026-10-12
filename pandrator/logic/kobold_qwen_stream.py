"""Owned read-ahead lifecycle for Kobold Qwen batch audio streams."""

import ctypes
import logging
import socket
import sys
from collections.abc import Callable, Iterator
from ctypes import wintypes
from functools import lru_cache
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

import requests

from .kobold_qwen_contracts import KoboldQwenBatchAudioEvent, KoboldQwenBatchMessage

logger = logging.getLogger(__name__)
_IS_WINDOWS = sys.platform == "win32"
_THREAD_TERMINATE = 0x0001
_ERROR_NOT_FOUND = 1168


@lru_cache(maxsize=1)
def _windows_kernel32() -> ctypes.CDLL:
    """Load and type the Windows APIs used to interrupt the owned reader."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.CancelSynchronousIo.argtypes = (wintypes.HANDLE,)
    kernel32.CancelSynchronousIo.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_last_error() -> int:
    return ctypes.get_last_error()


def _windows_api_error(api_name: str, error_code: int) -> OSError:
    return OSError(error_code, f"{api_name} failed.")


def _cancel_windows_reader_io(native_id: int | None) -> None:
    """Cancel pending synchronous I/O on this response's owned reader thread."""
    if not _IS_WINDOWS or native_id is None:
        return

    kernel32 = _windows_kernel32()
    handle = kernel32.OpenThread(_THREAD_TERMINATE, False, native_id)
    if not handle:
        raise _windows_api_error("OpenThread", _windows_last_error())

    try:
        if not kernel32.CancelSynchronousIo(handle):
            error_code = _windows_last_error()
            if error_code != _ERROR_NOT_FOUND:
                raise _windows_api_error("CancelSynchronousIo", error_code)
    finally:
        pending_error = sys.exception()
        try:
            closed = kernel32.CloseHandle(handle)
        except BaseException:
            if pending_error is None:
                raise
            logger.debug("Failed to close the Windows reader thread handle.", exc_info=True)
        else:
            if not closed:
                close_error = _windows_api_error("CloseHandle", _windows_last_error())
                if pending_error is None:
                    raise close_error
                logger.debug(
                    "Failed to close the Windows reader thread handle.",
                    exc_info=(
                        type(pending_error),
                        pending_error,
                        pending_error.__traceback__,
                    ),
                )


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
    public raw shutdown as a fallback. Before response admission, or when neither
    shutdown path is available, cleanup waits for the existing transport timeout.
    Response close remains the producer reader's responsibility.
    """
    if items_count <= 0:
        return

    messages: Queue[KoboldQwenBatchMessage] = Queue(maxsize=items_count + 2)
    stop_event = Event()
    response_lock = Lock()
    active_response: requests.Response | None = None

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
        nonlocal active_response
        with response_lock:
            active_response = response
        if response is not None and stop_event.is_set():
            interrupt(response)

    def interrupt_active_response() -> None:
        with response_lock:
            if active_response is not None:
                # Keep the producer from releasing the response back to its pool
                # until both owner-socket and reader-I/O interruption have finished.
                interrupt(active_response)
                _cancel_windows_reader_io(reader.native_id)

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
