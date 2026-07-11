"""Disposable authenticated API/static server for Playwright."""

from __future__ import annotations

import atexit
import shutil
import tempfile
from pathlib import Path

from waitress import serve

from pandrator.web.api import create_app
from pandrator.web.auth import AuthService


root = Path(tempfile.mkdtemp(prefix="pandrator-playwright-"))
atexit.register(lambda: shutil.rmtree(root, ignore_errors=True))
app = create_app(data_root=root, testing=False)
AuthService(app.extensions["pandrator"]["database"]).initialize_owner("pandrator-e2e")
serve(app, host="127.0.0.1", port=8098, threads=8)
