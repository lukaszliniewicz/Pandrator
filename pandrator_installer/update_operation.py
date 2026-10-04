"""Own update maintenance metadata and lifecycle leases through activation."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import TracebackType

from .lifecycle_guard import installation_lifecycle_guard
from .runtime_metadata_files import (
    RuntimeMetadataSnapshot,
    discard_runtime_metadata,
    read_runtime_metadata,
    runtime_metadata_guard,
    runtime_metadata_matches,
)


class UpdateOperation:
    """Keep an update marker owned until preparation or activation finishes."""

    def __init__(self, root: Path, version: str) -> None:
        self.root = root
        self.version = version
        self._leases = ExitStack()
        self._lease_held = False
        self._snapshot: RuntimeMetadataSnapshot | None = None
        self._activation_attempted = False
        self._finished = False
        self._cleanup_result = False

    @property
    def activation_attempted(self) -> bool:
        return self._activation_attempted

    def __enter__(self) -> UpdateOperation:
        if self._snapshot is not None or self._finished:
            raise RuntimeError("An update operation cannot be entered more than once.")
        try:
            self._leases.enter_context(installation_lifecycle_guard(self.root, shared=True))
            self._lease_held = True
            with runtime_metadata_guard(self.root):
                path = self.root / "maintenance.json"
                data = json.dumps(
                    {
                        "reason": "update",
                        "version": self.version,
                        "started_at": time.time(),
                        "operation_id": uuid.uuid4().hex,
                    }
                ).encode("utf-8")
                try:
                    handle = path.open("xb")
                except FileExistsError:
                    raise RuntimeError(
                        "An update maintenance marker already exists. Check the current "
                        "update or repair it before retrying."
                    ) from None
                created_identity: tuple[int, int] | None = None
                try:
                    with handle:
                        created = os.fstat(handle.fileno())
                        created_identity = (created.st_dev, created.st_ino)
                        handle.write(data)
                        handle.flush()
                    snapshot = read_runtime_metadata(path)
                    if (
                        snapshot is None
                        or snapshot.data != data
                        or snapshot.version[:2] != created_identity
                    ):
                        raise RuntimeError(
                            "Could not verify ownership of update maintenance metadata."
                        )
                    self._snapshot = snapshot
                except BaseException:
                    try:
                        if created_identity is not None:
                            current = path.lstat()
                            if (current.st_dev, current.st_ino) == created_identity:
                                path.unlink()
                    except FileNotFoundError:
                        pass
                    except BaseException:
                        logging.exception("Could not clean up failed update maintenance metadata.")
                    raise
            return self
        except BaseException:
            # Entry can fail while leaving the metadata guard after the marker
            # has been verified. __exit__ will not run for a failed __enter__.
            if self._snapshot is not None:
                try:
                    discard_runtime_metadata(self._snapshot)
                except BaseException:
                    logging.exception(
                        "Could not clear owned maintenance after update entry failed."
                    )
            try:
                self._leases.close()
            except BaseException:
                logging.exception("Could not close lifecycle leases after update entry failed.")
            finally:
                self._lease_held = False
            raise

    def activate(self) -> None:
        if self._activation_attempted:
            raise RuntimeError("Update activation has already been attempted.")
        if self._snapshot is None or self._finished:
            raise RuntimeError("Update activation requires an unfinished maintenance operation.")
        self._activation_attempted = True
        try:
            self._leases.close()
        finally:
            self._lease_held = False
        try:
            self._leases.enter_context(installation_lifecycle_guard(self.root, shared=False))
            self._lease_held = True
            with runtime_metadata_guard(self.root):
                if not runtime_metadata_matches(self._snapshot):
                    raise RuntimeError(
                        "Update maintenance ownership changed; refusing to activate."
                    )
        except BaseException:
            try:
                self._leases.close()
            except BaseException:
                logging.exception(
                    "Could not close lifecycle leases after update activation failed."
                )
            finally:
                self._lease_held = False
            raise

    def finish(self) -> bool:
        if self._finished:
            return self._cleanup_result
        try:
            if self._snapshot is not None:
                if not self._lease_held:
                    self._leases.enter_context(installation_lifecycle_guard(self.root, shared=True))
                    self._lease_held = True
                self._cleanup_result = discard_runtime_metadata(self._snapshot)
            return self._cleanup_result
        finally:
            self._finished = True
            try:
                self._leases.close()
            finally:
                self._lease_held = False

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            cleaned = self._cleanup_result if self._finished else self.finish()
        except BaseException as error:
            if exc_value is not None:
                logging.exception("Could not finish update maintenance during exception cleanup.")
                return
            raise RuntimeError("Could not finish update maintenance cleanup.") from error
        if not cleaned:
            if exc_value is not None:
                logging.warning("Update maintenance ownership changed; its marker was preserved.")
            else:
                raise RuntimeError(
                    "Update maintenance ownership changed; its marker was preserved."
                )
