"""Conservative retention for compactable metadata and disposable files."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete

from pandrator.runtime import DataPaths

from .database import Database
from .models import AppSettingHistory, JobEvent, SessionSettingHistory, utcnow


def apply_retention(database: Database, paths: DataPaths, days: int) -> dict[str, int]:
    days = max(1, min(3650, int(days)))
    cutoff = utcnow() - timedelta(days=days)
    with database.session() as session:
        events = session.execute(delete(JobEvent).where(JobEvent.created_at < cutoff)).rowcount or 0
        app_history = session.execute(delete(AppSettingHistory).where(AppSettingHistory.created_at < cutoff)).rowcount or 0
        session_history = session.execute(delete(SessionSettingHistory).where(SessionSettingHistory.created_at < cutoff)).rowcount or 0
    files = 0
    cutoff_timestamp = cutoff.timestamp()
    for root in (paths.temporary, paths.logs):
        resolved_root = root.resolve()
        for candidate in root.rglob("*") if root.is_dir() else ():
            try:
                resolved = candidate.resolve()
                if not resolved.is_relative_to(resolved_root) or not candidate.is_file() or candidate.stat().st_mtime >= cutoff_timestamp:
                    continue
                candidate.unlink()
                files += 1
            except OSError:
                continue
    return {"job_events": events, "app_setting_history": app_history, "session_setting_history": session_history, "files": files}
