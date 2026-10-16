"""Indexed EPUB document lookup for footnote and note links."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from urllib.parse import unquote


def normalize_archive_path(value: str) -> str:
    """Normalize an EPUB archive path with the historical note-link rules."""
    decoded = unquote(str(value or "").split("?", 1)[0]).replace("\\", "/")
    normalized = posixpath.normpath(decoded).lstrip("/")
    return "" if normalized == "." else normalized.casefold()


class NoteDocumentIndex:
    """Resolve archive paths and unique basename fallbacks for one parsed book."""

    def __init__(self, parsed_documents: Mapping[str, object]) -> None:
        self._unique_paths: dict[str, str] = {}
        self._ambiguous_paths: set[str] = set()
        self._unique_basenames: dict[str, str] = {}
        self._ambiguous_basenames: set[str] = set()

        for original_key in parsed_documents:
            normalized_path = normalize_archive_path(str(original_key))
            self._add_unique(
                self._unique_paths,
                self._ambiguous_paths,
                normalized_path,
                original_key,
            )
            basename = posixpath.basename(normalized_path)
            self._add_unique(
                self._unique_basenames,
                self._ambiguous_basenames,
                basename,
                original_key,
            )

    @staticmethod
    def _add_unique(
        unique_values: dict[str, str],
        ambiguous_values: set[str],
        normalized_value: str,
        original_key: str,
    ) -> None:
        if normalized_value in ambiguous_values:
            return
        if normalized_value in unique_values:
            del unique_values[normalized_value]
            ambiguous_values.add(normalized_value)
            return
        unique_values[normalized_value] = original_key

    def resolve(self, referencing_href: str, target_file: str) -> str | None:
        """Resolve a relative or archive-root note path, with unique fallback."""
        if target_file:
            resolved = posixpath.normpath(
                posixpath.join(
                    posixpath.dirname(str(referencing_href or "").replace("\\", "/")),
                    unquote(str(target_file)).replace("\\", "/"),
                )
            )
        else:
            resolved = referencing_href

        normalized = normalize_archive_path(resolved)
        if (
            normalized not in self._ambiguous_paths
            and normalized in self._unique_paths
        ):
            return self._unique_paths[normalized]

        basename = posixpath.basename(normalized)
        if (
            basename not in self._ambiguous_basenames
            and basename in self._unique_basenames
        ):
            return self._unique_basenames[basename]
        return None
