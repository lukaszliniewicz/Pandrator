"""File-name stems for a project's exports."""

from __future__ import annotations

from werkzeug.utils import secure_filename

EXPORT_STEM_MAX_CHARS = 80


def export_file_stem(name: str, fallback: str) -> str:
    """Return a safe, bounded file-name stem for a project's exports.

    Project names may be 255 characters. Exports append a track or mode, an
    extension and sometimes a version suffix, and Windows counts the whole
    path against 260 characters, so the stem is capped.
    """
    stem = secure_filename(name)[:EXPORT_STEM_MAX_CHARS].rstrip("._-")
    return stem or fallback
