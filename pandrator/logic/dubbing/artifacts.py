"""Artifact discovery helpers for Pandrator-native dubbing workflows."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

DEFAULT_SRT_ROLES: tuple[str, ...] = (
    "reviewed_translated_srt",
    "translated_srt",
    "reviewed_corrected_srt",
    "corrected_srt",
    "reviewed_source_srt",
    "transcribed_srt",
    "source_srt",
)
DEFAULT_SUFFIX_ROLE_LOOKUP: dict[str, tuple[str, ...]] = {
    "_speech_blocks.json": ("speech_blocks",),
    "_equalized.srt": ("equalized_srt",),
}

GetActiveArtifact = Callable[[str, list[str]], str]
GetSessionPath = Callable[[str], str]


def resolve_active_artifact_path(
    session_name: str,
    roles: Sequence[str],
    get_active_artifact: GetActiveArtifact,
    *,
    must_exist: bool = True,
    suffixes: Sequence[str] = (),
) -> str:
    if not session_name or not roles:
        return ""

    normalized_roles = [str(role or "").strip() for role in roles if str(role or "").strip()]
    if not normalized_roles:
        return ""

    try:
        artifact_path = get_active_artifact(session_name, normalized_roles)
    except Exception as error:
        logger.debug("Could not resolve active dubbing artifact for roles %s: %s", normalized_roles, error)
        return ""

    normalized_path = str(artifact_path or "").strip()
    if not normalized_path:
        return ""
    if must_exist and not os.path.exists(normalized_path):
        return ""
    if suffixes and not normalized_path.lower().endswith(tuple(str(suffix).lower() for suffix in suffixes)):
        return ""
    return normalized_path


def find_latest_srt(
    session_name: str,
    session_dir: str,
    get_session_path: GetSessionPath,
    get_active_artifact: GetActiveArtifact,
    *,
    must_not_be_equalized: bool = False,
) -> str | None:
    preferred_roles = DEFAULT_SRT_ROLES
    if not must_not_be_equalized:
        preferred_roles = ("equalized_srt", *preferred_roles)

    artifact_path = resolve_active_artifact_path(
        session_name,
        preferred_roles,
        get_active_artifact,
        suffixes=(".srt",),
    )
    if artifact_path and not (must_not_be_equalized and artifact_path.lower().endswith("_equalized.srt")):
        return artifact_path

    search_dirs: list[str] = []
    if session_dir:
        search_dirs.append(session_dir)

    root_session_dir = get_session_path(session_name)
    if root_session_dir and root_session_dir not in search_dirs:
        search_dirs.append(root_session_dir)

    srt_files: list[tuple[str, float, int]] = []
    for priority, directory in enumerate(search_dirs):
        if not os.path.isdir(directory):
            continue

        for file_name in os.listdir(directory):
            file_name_lower = file_name.lower()
            if not file_name_lower.endswith(".srt"):
                continue
            if must_not_be_equalized and file_name_lower.endswith("_equalized.srt"):
                continue

            full_path = os.path.join(directory, file_name)
            if os.path.isfile(full_path):
                srt_files.append((full_path, os.path.getmtime(full_path), -priority))

    if not srt_files:
        return None

    latest_srt, _, _ = max(srt_files, key=lambda item: (item[1], item[2], item[0].lower()))
    return latest_srt


def discover_latest_file_with_suffix(
    session_name: str,
    directory: str,
    suffix: str,
    get_active_artifact: GetActiveArtifact,
    *,
    role_lookup: Mapping[str, Sequence[str]] | None = None,
) -> str | None:
    normalized_suffix = str(suffix or "").lower()
    active_role_lookup = role_lookup or DEFAULT_SUFFIX_ROLE_LOOKUP
    roles = active_role_lookup.get(normalized_suffix)
    if roles:
        artifact_path = resolve_active_artifact_path(session_name, roles, get_active_artifact)
        if artifact_path:
            return artifact_path

    if not os.path.isdir(directory):
        return None

    candidates: list[str] = []
    for name in os.listdir(directory):
        if not name.lower().endswith(normalized_suffix):
            continue

        full_path = os.path.join(directory, name)
        if os.path.isfile(full_path):
            candidates.append(full_path)

    if not candidates:
        return None

    return max(candidates, key=lambda path: (os.path.getmtime(path), path.lower()))
