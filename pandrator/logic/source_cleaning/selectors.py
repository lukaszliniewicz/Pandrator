from __future__ import annotations

import re
from typing import Any

from .models import SourceBlock


SUPPORTED_SELECTOR_KEYS = {
    "href",
    "href_regex",
    "tag",
    "class",
    "classes",
    "element_id",
    "element_id_regex",
    "role",
    "roles",
    "exclude_hrefs",
    "exclude_tags",
    "exclude_classes",
    "exclude_roles",
    "text_regex",
    "start_line",
    "end_line",
    "page",
    "source_method",
    "min_role_score",
}


def selector_supported_keys() -> list[str]:
    return sorted(SUPPORTED_SELECTOR_KEYS)


def blocks_matching_selector(blocks: list[SourceBlock], selector: dict[str, Any]) -> list[SourceBlock]:
    if not isinstance(selector, dict) or not selector:
        return []

    return [
        block
        for block in blocks
        if block_matches_selector(block, selector)
    ]


def block_matches_selector(block: SourceBlock, selector: dict[str, Any]) -> bool:
    href = selector.get("href")
    if href is not None and str(block.href or "") != str(href):
        return False

    href_regex = selector.get("href_regex")
    if href_regex and not _safe_search(str(href_regex), str(block.href or "")):
        return False

    tag = selector.get("tag")
    if tag is not None and str(block.tag or "").lower() != str(tag).lower():
        return False

    element_id = selector.get("element_id")
    if element_id is not None and str(block.element_id or "") != str(element_id):
        return False

    element_id_regex = selector.get("element_id_regex")
    if element_id_regex and not _safe_search(str(element_id_regex), str(block.element_id or "")):
        return False

    class_value = selector.get("class")
    if class_value is not None and str(class_value) not in block.classes:
        return False

    classes = selector.get("classes")
    if classes:
        required_classes = {str(item) for item in _coerce_list(classes)}
        if not required_classes.issubset(set(block.classes)):
            return False

    role = selector.get("role")
    if role is not None and str(role) not in block.role_candidates:
        return False

    roles = selector.get("roles")
    if roles:
        required_roles = {str(item) for item in _coerce_list(roles)}
        if not required_roles.issubset(set(block.role_candidates)):
            return False

    excluded_hrefs = {str(item) for item in _coerce_list(selector.get("exclude_hrefs"))}
    if excluded_hrefs and str(block.href or "") in excluded_hrefs:
        return False

    excluded_tags = {str(item).lower() for item in _coerce_list(selector.get("exclude_tags"))}
    if excluded_tags and str(block.tag or "").lower() in excluded_tags:
        return False

    excluded_classes = {str(item) for item in _coerce_list(selector.get("exclude_classes"))}
    if excluded_classes.intersection(block.classes):
        return False

    excluded_roles = {str(item) for item in _coerce_list(selector.get("exclude_roles"))}
    if excluded_roles.intersection(block.role_candidates):
        return False

    text_regex = selector.get("text_regex")
    if text_regex and not _safe_search(str(text_regex), block.text):
        return False

    start_line = selector.get("start_line")
    if start_line is not None and block.line_end < int(start_line):
        return False

    end_line = selector.get("end_line")
    if end_line is not None and block.line_start > int(end_line):
        return False

    page = selector.get("page")
    if page is not None and block.page != int(page):
        return False

    source_method = selector.get("source_method")
    if source_method is not None and str(block.attributes.get("source_method") or "") != str(source_method):
        return False

    min_role_score = selector.get("min_role_score")
    if isinstance(min_role_score, dict):
        for role_name, minimum in min_role_score.items():
            if block.role_score(str(role_name)) < float(minimum):
                return False

    return True


def selector_summary(selector: dict[str, Any]) -> dict[str, Any]:
    return {
        key: selector.get(key)
        for key in selector_supported_keys()
        if selector.get(key) not in (None, "", [], {})
    }


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _safe_search(pattern: str, text: str) -> bool:
    try:
        return re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE) is not None
    except re.error:
        return False
