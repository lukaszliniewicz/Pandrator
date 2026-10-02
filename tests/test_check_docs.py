"""Private maintainer records cannot masquerade as public documentation."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.check_docs import check_repository


@pytest.fixture
def documentation_repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/README.md").write_text("# Documentation\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(
        "/reviews/\n/review-notes/\n/.local-notes/\n/release-acceptance.md\n/docs/local-only.md\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize(
    "relative",
    [
        "reviews/audit.md",
        "review-notes/screenshot.png",
        ".local-notes/result.json",
        "release-acceptance.md",
    ],
)
def test_force_added_internal_material_is_rejected(
    documentation_repository: Path, relative: str
) -> None:
    path = documentation_repository / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"local evidence")
    subprocess.run(["git", "add", "--force", relative], cwd=documentation_repository, check=True)

    errors = check_repository(documentation_repository)

    assert any(f"internal material must remain untracked: {relative}" in error for error in errors)


def test_ignored_local_notes_do_not_need_public_index_entries(
    documentation_repository: Path,
) -> None:
    (documentation_repository / "docs/local-only.md").write_text("# Local record\n", encoding="utf-8")
    (documentation_repository / "review-notes").mkdir()
    (documentation_repository / "review-notes/result.md").write_text("# Local result\n", encoding="utf-8")

    assert check_repository(documentation_repository) == []


def test_public_link_to_private_evidence_is_rejected(documentation_repository: Path) -> None:
    (documentation_repository / "review-notes").mkdir()
    (documentation_repository / "review-notes/result.md").write_text("# Local result\n", encoding="utf-8")
    (documentation_repository / "docs/README.md").write_text(
        "# Documentation\n\n[Evidence](../review-notes/result.md)\n", encoding="utf-8"
    )

    errors = check_repository(documentation_repository)

    assert any("internal material is not a public link target" in error for error in errors)


def test_public_link_to_other_ignored_markdown_is_rejected(documentation_repository: Path) -> None:
    (documentation_repository / "docs/local-only.md").write_text("# Local record\n", encoding="utf-8")
    (documentation_repository / "docs/README.md").write_text(
        "# Documentation\n\n[Local record](local-only.md)\n", encoding="utf-8"
    )

    errors = check_repository(documentation_repository)

    assert any("Markdown link target is not public" in error for error in errors)


def test_public_guidance_and_repository_links_are_allowed(documentation_repository: Path) -> None:
    (documentation_repository / "docs/guide.md").write_text("# Guide\n", encoding="utf-8")
    (documentation_repository / "docs/README.md").write_text(
        "# Documentation\n\n[Guide](guide.md#guide) · [Repository](../)\n", encoding="utf-8"
    )

    assert check_repository(documentation_repository) == []
