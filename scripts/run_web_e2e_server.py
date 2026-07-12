"""Disposable authenticated API/static server for Playwright."""

from __future__ import annotations

import atexit
import shutil
import tempfile
import threading
from pathlib import Path

from waitress import serve

from pandrator.web.api import create_app
from pandrator.web.auth import AuthService
from pandrator.web.jobs import JobQueue, Worker, noop_handler
from pandrator.web.workflow_handlers import WorkflowHandlers


root = Path(tempfile.mkdtemp(prefix="pandrator-playwright-"))
atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
app = create_app(data_root=root, testing=False)
AuthService(app.extensions["pandrator"]["database"]).initialize_owner("pandrator-e2e")
database = app.extensions["pandrator"]["database"]
paths = app.extensions["pandrator"]["paths"]
worker = Worker(
    JobQueue(database),
    "playwright-worker",
    {"noop": noop_handler, **WorkflowHandlers(database, paths).handlers()},
)
worker_thread = threading.Thread(target=worker.run_forever, kwargs={"poll_interval": 0.05}, daemon=True)
worker_thread.start()


def stop_worker() -> None:
    worker.stop()
    worker_thread.join(timeout=3)


atexit.register(stop_worker)
serve(app, host="127.0.0.1", port=8098, threads=8)
