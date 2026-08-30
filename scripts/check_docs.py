"""Validate the tracked Markdown documentation without network access."""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urlsplit

_LINK_RE = re.compile(
    r"!?\[[^\]]*\]\((?P<target><[^>]+>|[^\s)]+)(?:\s+[^)]*)?\)",
    re.MULTILINE,
)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(?P<title>.+?)\s*#*\s*$", re.MULTILINE)
_INLINE_LINK_RE = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_FORBIDDEN_PUBLIC_DOCUMENTS = frozenset(
    {
        "PASSIVE_DISPATCHER_FIELD_NOTES.md",
        "SUBTITLE_PIPELINE_REVIEW.md",
    }
)
_REQUIRED_INDEX = Path("docs/README.md")


def _tracked_markdown(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
            "--",
            "*.md",
            "*.markdown",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = [
        Path(raw.decode("utf-8"))
        for raw in result.stdout.split(b"\0")
        if raw and (root / Path(raw.decode("utf-8"))).is_file()
    ]
    return tuple(sorted(paths))


def _heading_slug(title: str) -> str:
    visible = _INLINE_LINK_RE.sub(r"\1", title)
    visible = _HTML_TAG_RE.sub("", visible)
    visible = visible.replace("`", "").replace("*", "").replace("_", "")
    normalized = "".join(
        character
        for character in visible.casefold()
        if character.isalnum() or character in {" ", "-"}
    )
    return re.sub(r"\s", "-", normalized.strip())


def _anchors(path: Path) -> frozenset[str]:
    counts: Counter[str] = Counter()
    anchors: set[str] = set()
    text = path.read_text(encoding="utf-8")
    for match in _HEADING_RE.finditer(text):
        base = _heading_slug(match.group("title"))
        if not base:
            continue
        suffix = counts[base]
        counts[base] += 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return frozenset(anchors)


def _local_target(
    *,
    source: Path,
    target: str,
    root: Path,
) -> tuple[Path, str] | None:
    cleaned = (
        target[1:-1] if target.startswith("<") and target.endswith(">") else target
    )
    parsed = urlsplit(cleaned)
    if parsed.scheme or parsed.netloc:
        return None
    if cleaned.startswith("//"):
        return None
    raw_path, separator, raw_fragment = cleaned.partition("#")
    if raw_path.startswith("/"):
        raise ValueError("repository-local links must be relative")
    selected = source if not raw_path else source.parent / unquote(raw_path)
    resolved = selected.resolve(strict=False)
    repository = root.resolve(strict=True)
    if not resolved.is_relative_to(repository):
        raise ValueError("link escapes the repository")
    return resolved, unquote(raw_fragment) if separator else ""


def check_repository(root: Path) -> list[str]:
    repository = root.resolve(strict=True)
    documents = _tracked_markdown(repository)
    errors: list[str] = []
    resolved_documents = {repository / path for path in documents}

    if repository / _REQUIRED_INDEX not in resolved_documents:
        errors.append(f"missing required documentation index: {_REQUIRED_INDEX}")

    for relative in documents:
        if relative.name in _FORBIDDEN_PUBLIC_DOCUMENTS or relative.name.startswith(
            "RELEASE_NOTES"
        ):
            errors.append(f"internal or release-only document is tracked: {relative}")

    indexed_pages: set[Path] = set()
    for relative in documents:
        source = repository / relative
        text = source.read_text(encoding="utf-8")
        for match in _LINK_RE.finditer(text):
            raw_target = match.group("target")
            try:
                target = _local_target(
                    source=source, target=raw_target, root=repository
                )
            except ValueError as error:
                errors.append(f"{relative}: {raw_target}: {error}")
                continue
            if target is None:
                continue
            target_path, fragment = target
            if not target_path.exists():
                errors.append(f"{relative}: missing local link target {raw_target}")
                continue
            if relative == _REQUIRED_INDEX and target_path.suffix.casefold() in {
                ".md",
                ".markdown",
            }:
                indexed_pages.add(target_path)
            if fragment:
                heading_path = target_path
                if heading_path.is_dir():
                    heading_path = heading_path / "README.md"
                if heading_path.suffix.casefold() not in {".md", ".markdown"}:
                    errors.append(
                        f"{relative}: fragment points to a non-Markdown target {raw_target}"
                    )
                elif fragment.casefold() not in _anchors(heading_path):
                    errors.append(f"{relative}: missing heading fragment {raw_target}")

    for page in sorted((repository / "docs").rglob("*.md")):
        if page == repository / _REQUIRED_INDEX:
            continue
        if page not in indexed_pages:
            errors.append(
                f"{page.relative_to(repository)}: public page is not linked from docs/README.md"
            )
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate tracked Markdown paths, anchors, and public navigation."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Repository root (defaults to the checkout containing this script).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors = check_repository(args.root)
    if errors:
        for error in errors:
            print(f"documentation error: {error}")
        return 1
    document_count = len(_tracked_markdown(args.root.resolve(strict=True)))
    print(f"Documentation check passed for {document_count} tracked Markdown files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
