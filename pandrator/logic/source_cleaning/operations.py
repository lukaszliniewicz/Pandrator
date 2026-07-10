from __future__ import annotations

import difflib
import json
import os
from typing import Any

from .models import CleaningResult, SourceBlock, SourceDocument
from .selectors import blocks_matching_selector, selector_summary


ALLOWED_METADATA_KEYS = {
    "title",
    "album",
    "artist",
    "author",
    "genre",
    "language",
    "series",
    "publisher",
    "date",
}


def apply_cleaning_operations(
    document: SourceDocument,
    operations: list[dict[str, Any]],
    default_metadata: dict[str, str] | None = None,
    output_dir: str | None = None,
    max_replace_lines: int = 5,
    max_replacement_chars: int = 1000,
) -> CleaningResult:
    """Applies targeted LLM-proposed operations deterministically."""
    metadata = dict(default_metadata or {})
    deleted_block_ids: set[str] = set()
    chapter_marks: dict[str, str] = {}
    replacements: dict[str, str] = {}
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[str] = []

    block_ids = {block.block_id for block in document.blocks}

    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, dict):
            skipped.append({"index": index, "operation": operation, "reason": "operation is not an object"})
            continue

        op = str(operation.get("op") or "").strip()
        if op == "set_metadata":
            updates = _metadata_updates(operation)
            if not updates:
                skipped.append({"index": index, "operation": operation, "reason": "no allowed metadata keys"})
                continue
            metadata.update(updates)
            applied.append(_applied(index, operation, {"metadata_keys": sorted(updates)}))
            continue

        if op == "delete_range":
            start_line = operation.get("start_line")
            end_line = operation.get("end_line")
            if start_line is None or end_line is None:
                skipped.append({"index": index, "operation": operation, "reason": "start_line/end_line required"})
                continue
            targets = document.blocks_in_line_range(int(start_line), int(end_line))
            if not targets:
                skipped.append({"index": index, "operation": operation, "reason": "line range matched no blocks"})
                continue
            for block in targets:
                deleted_block_ids.add(block.block_id)
            applied.append(_applied(index, operation, {"deleted_blocks": len(targets)}))
            continue

        if op == "delete_blocks":
            requested_ids = [str(block_id) for block_id in operation.get("block_ids", [])]
            targets = [block_id for block_id in requested_ids if block_id in block_ids]
            missing = [block_id for block_id in requested_ids if block_id not in block_ids]
            if not targets:
                skipped.append({"index": index, "operation": operation, "reason": "no valid block_ids"})
                continue
            deleted_block_ids.update(targets)
            details: dict[str, Any] = {"deleted_blocks": len(targets)}
            if missing:
                details["missing_block_ids"] = missing
            applied.append(_applied(index, operation, details))
            continue

        if op == "delete_by_selector":
            selector = operation.get("selector")
            if not isinstance(selector, dict) or not selector:
                skipped.append({"index": index, "operation": operation, "reason": "selector object required"})
                continue
            targets = blocks_matching_selector(document.blocks, selector)
            if not targets:
                skipped.append({"index": index, "operation": operation, "reason": "selector matched no blocks"})
                continue
            for block in targets:
                deleted_block_ids.add(block.block_id)
            applied.append(
                _applied(
                    index,
                    operation,
                    {
                        "deleted_blocks": len(targets),
                        "selector": selector_summary(selector),
                        "first_line": targets[0].line_start,
                        "last_line": targets[-1].line_end,
                    },
                )
            )
            continue

        if op == "mark_chapter":
            block = _resolve_operation_block(document, operation)
            if block is None:
                skipped.append({"index": index, "operation": operation, "reason": "chapter target not found"})
                continue
            title = str(operation.get("title") or "").strip() or block.text.strip()
            chapter_marks[block.block_id] = title
            applied.append(_applied(index, operation, {"block_id": block.block_id, "title": title}))
            continue

        if op == "mark_chapters_by_selector":
            selector = operation.get("selector")
            if not isinstance(selector, dict) or not selector:
                skipped.append({"index": index, "operation": operation, "reason": "selector object required"})
                continue
            targets = blocks_matching_selector(document.blocks, selector)
            if not targets:
                skipped.append({"index": index, "operation": operation, "reason": "selector matched no blocks"})
                continue
            for block in targets:
                chapter_marks[block.block_id] = block.text.strip()
            applied.append(
                _applied(
                    index,
                    operation,
                    {
                        "marked_chapters": len(targets),
                        "selector": selector_summary(selector),
                        "first_line": targets[0].line_start,
                        "last_line": targets[-1].line_end,
                    },
                )
            )
            continue

        if op == "replace_range":
            start_line = operation.get("start_line")
            end_line = operation.get("end_line")
            replacement = str(operation.get("replacement") or "")
            if start_line is None or end_line is None:
                skipped.append({"index": index, "operation": operation, "reason": "start_line/end_line required"})
                continue
            targets = document.blocks_in_line_range(int(start_line), int(end_line))
            if not targets:
                skipped.append({"index": index, "operation": operation, "reason": "line range matched no blocks"})
                continue
            if len(targets) > max_replace_lines or len(replacement) > max_replacement_chars:
                skipped.append(
                    {
                        "index": index,
                        "operation": operation,
                        "reason": "replace_range exceeds guard limits",
                        "matched_lines": len(targets),
                        "replacement_chars": len(replacement),
                    }
                )
                continue
            first = targets[0]
            replacements[first.block_id] = replacement.strip()
            for block in targets[1:]:
                deleted_block_ids.add(block.block_id)
            applied.append(
                _applied(
                    index,
                    operation,
                    {"replaced_blocks": len(targets), "target_block_id": first.block_id},
                )
            )
            continue

        skipped.append({"index": index, "operation": operation, "reason": f"unsupported operation: {op}"})

    cleaned_lines: list[str] = []
    last_output_block_id = ""
    joined_page_continuations = 0
    for block in document.blocks:
        if block.block_id in deleted_block_ids:
            continue
        text = replacements.get(block.block_id, block.text).strip()
        if not text:
            continue
        is_chapter = block.block_id in chapter_marks
        if is_chapter:
            title = chapter_marks[block.block_id].strip() or text
            text = title if title.startswith("[[Chapter]]") else f"[[Chapter]]{title}"
        continuation_from = str(block.attributes.get("continuation_from_block_id") or "")
        continuation_mode = str(block.attributes.get("continuation_join") or "")
        if (
            not is_chapter
            and continuation_from
            and continuation_from == last_output_block_id
            and cleaned_lines
        ):
            cleaned_lines[-1] = _join_page_continuation(
                cleaned_lines[-1], text, continuation_mode
            )
            joined_page_continuations += 1
        else:
            cleaned_lines.append(text)
        last_output_block_id = block.block_id

    original_lines = document.plain_lines()
    cleaned_text = "\n\n".join(cleaned_lines)
    diff_text = _build_diff(original_lines, cleaned_lines)

    report = {
        "source_type": document.source_type,
        "source_path": document.source_path,
        "original_block_count": len(original_lines),
        "cleaned_block_count": len(cleaned_lines),
        "deleted_block_count": len(deleted_block_ids),
        "chapter_count": sum(1 for line in cleaned_lines if line.startswith("[[Chapter]]")),
        "page_continuation_join_count": joined_page_continuations,
        "applied_operation_count": len(applied),
        "skipped_operation_count": len(skipped),
        "metadata": metadata,
        "warnings": warnings,
    }

    result = CleaningResult(
        cleaned_text=cleaned_text,
        metadata=metadata,
        applied_operations=applied,
        skipped_operations=skipped,
        warnings=warnings,
        deleted_block_ids=sorted(deleted_block_ids),
        diff_text=diff_text,
        report=report,
    )

    if output_dir:
        write_cleaning_artifacts(document, operations, result, output_dir)

    return result


def write_cleaning_artifacts(
    document: SourceDocument,
    operations: list[dict[str, Any]],
    result: CleaningResult,
    output_dir: str,
) -> dict[str, str]:
    os.makedirs(output_dir, exist_ok=True)
    paths = {
        "raw_index": os.path.join(output_dir, "raw_index.json"),
        "raw_text": os.path.join(output_dir, "raw_text.txt"),
        "cleaned_text": os.path.join(output_dir, "cleaned_text.txt"),
        "cleaning_rules": os.path.join(output_dir, "cleaning_rules.json"),
        "cleaning_report": os.path.join(output_dir, "cleaning_report.json"),
        "diff": os.path.join(output_dir, "diff.patch"),
    }

    _write_json(paths["raw_index"], document.to_dict())
    _write_text(paths["raw_text"], document.plain_text())
    _write_text(paths["cleaned_text"], result.cleaned_text)
    _write_json(
        paths["cleaning_rules"],
        {
            "operations": operations,
            "applied_operations": result.applied_operations,
            "skipped_operations": result.skipped_operations,
        },
    )
    _write_json(paths["cleaning_report"], result.report)
    _write_text(paths["diff"], result.diff_text)
    return paths


def _metadata_updates(operation: dict[str, Any]) -> dict[str, str]:
    updates: dict[str, str] = {}
    metadata_payload = operation.get("metadata")
    if isinstance(metadata_payload, dict):
        items = metadata_payload.items()
    else:
        items = operation.items()

    for key, value in items:
        normalized_key = str(key or "").strip()
        if normalized_key not in ALLOWED_METADATA_KEYS:
            continue
        normalized_value = str(value or "").strip()
        if normalized_value:
            updates[normalized_key] = normalized_value

    if "author" in updates and "artist" not in updates:
        updates["artist"] = updates["author"]

    return updates


def _resolve_operation_block(document: SourceDocument, operation: dict[str, Any]) -> SourceBlock | None:
    block_id = str(operation.get("block_id") or "").strip()
    if block_id:
        return document.block_by_id(block_id)

    line = operation.get("line")
    if line is None:
        line = operation.get("line_start")
    if line is None:
        line = operation.get("start_line")
    if line is None:
        return None

    blocks = document.blocks_in_line_range(int(line), int(line))
    return blocks[0] if blocks else None


def _applied(index: int, operation: dict[str, Any], details: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": index,
        "operation": operation,
        "details": details,
    }


def _build_diff(original_lines: list[str], cleaned_lines: list[str]) -> str:
    return "\n".join(
        difflib.unified_diff(
            original_lines,
            cleaned_lines,
            fromfile="raw_text",
            tofile="cleaned_text",
            lineterm="",
        )
    )


def _write_json(path: str, payload: Any):
    with open(path, "w", encoding="utf-8") as file_handle:
        json.dump(payload, file_handle, indent=2, ensure_ascii=False)


def _write_text(path: str, text: str):
    with open(path, "w", encoding="utf-8", newline="\n") as file_handle:
        file_handle.write(text)


def _join_page_continuation(previous: str, current: str, mode: str) -> str:
    previous_text = str(previous or "").rstrip()
    current_text = str(current or "").lstrip()
    if mode == "remove_hyphen" and previous_text.endswith(("-", "\u00ad")):
        return f"{previous_text[:-1]}{current_text}"
    return f"{previous_text} {current_text}".strip()
