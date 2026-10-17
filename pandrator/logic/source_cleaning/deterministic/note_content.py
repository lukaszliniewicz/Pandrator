"""Resolve note bodies within their source container without crossing notes."""

from __future__ import annotations

import re
from urllib.parse import unquote

_MARKER = re.compile(r"(?:\[?\(?\d+[.)\]]?|[A-Za-z]|[*†‡↩↑])")


def _backlink(part: dict) -> bool:
    if "backlink" in str(part.get("epub_type") or "").split():
        return True
    identity = re.sub(r"[^\w]", "", str(part.get("id") or "").casefold())
    fragment = unquote(str(part.get("href") or "").partition("#")[2]).casefold()
    target = re.sub(r"[^\w]", "", fragment)
    return bool(identity) and target != identity and re.sub(
        r"backlink|anchor|back|ref", "", target,
    ) == identity


def _marker_only(block: dict) -> bool:
    parts = block.get("parts") or []
    visible = [part for part in parts if str(part.get("content") or "").strip()]
    return bool(visible) and all(part.get("type") == "anchor" for part in visible) and (
        all(_backlink(part) for part in visible)
        or bool(_MARKER.fullmatch(str(block.get("text") or "").strip()))
    )


def get_footnote_text_multi_block(
    blocks: list[dict], start_idx: int,
) -> tuple[str, list[int]]:
    """Read a wrapper's descendants, or an unwrapped note up to the next ID.

    Parser element paths distinguish sibling note containers even when their
    IDs live on inline backlink anchors. A leaf marker's continuation stays
    inside its enclosing block. Legacy blocks without paths retain the
    original ID-delimited fallback.
    """
    start = blocks[start_idx]
    start_text = str(start.get("text") or "").strip()
    path = tuple(start.get("element_path") or ())
    end = start_idx + 1
    descendants = False
    if path:
        while end < len(blocks):
            following_path = tuple(blocks[end].get("element_path") or ())
            if following_path[:len(path)] != path:
                break
            descendants |= len(following_path) > len(path)
            end += 1
    if not descendants:
        if start_text and not _marker_only(start):
            return start_text, [start_idx]
        end = start_idx + 1
        parent_path = path[:-1]
        while end < len(blocks):
            following = blocks[end]
            following_path = tuple(following.get("element_path") or ())
            if parent_path and following_path[:len(parent_path)] != parent_path:
                break
            if following.get("id") or following.get("nested_ids"):
                break
            end += 1

    consumed = list(range(start_idx, end))
    body = [
        str(blocks[index].get("text") or "").strip()
        for index in consumed if not _marker_only(blocks[index])
    ]
    text = " ".join(part for part in body if part)
    return text or start_text, consumed
