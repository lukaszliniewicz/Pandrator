"""Owned read-ahead lifecycle for Kobold Qwen batch audio streams."""

import logging
import sys
from collections.abc import Callable, Iterator
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread

import requests

from .kobold_qwen_contracts import KoboldQwenBatchAudioEvent, KoboldQwenBatchMessage

logger = logging.getLogger(__name__)


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

    Public raw shutdown can interrupt an admitted response body. Before a
    response arrives, or without that capability, cleanup waits for the existing
    transport timeout. Response close remains the producer reader's responsibility.
    """
    if items_count <= 0:
        return

    messages: Queue[KoboldQwenBatchMessage] = Queue(maxsize=items_count + 2)
    stop_event = Event()
    response_lock = Lock()
    active_response: requests.Response | None = None

    def interrupt(response: requests.Response) -> None:
        shutdown = getattr(response.raw, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
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
            response = active_response
        if response is not None:
            interrupt(response)

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
