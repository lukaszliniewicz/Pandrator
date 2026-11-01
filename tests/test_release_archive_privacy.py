from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from scripts import audit_python_distributions as auditor
from scripts import build_release_packages as builder

PRIVATE_PATHS = (
    ".local-notes/local-review.md",
    "review-notes/fixture.png",
    "reviews/result.json",
    "AGENTS.md",
    "release-acceptance.md",
    "docs/development/from-source.md",
    "docs/plans/local-plan.md",
    "docs/research/measurements.json",
    "docs/reports/acceptance.png",
    ".env",
    ".env.example",
    "runtime/.env.local",
    "tmp/local-evaluation.json",
    "__pycache__/code.pyc",
    "backend_architecture.md",
    "implementation-plan.md",
    "docs/qualification/result.md",
)
PUBLIC_PATHS = ("docs/guides/guide.md", "README.md", "code.py", "config.json")


def populate_tree(root: Path) -> set[str]:
    public_members = set()
    for prefix in ("", "Pandrator/"):
        for relative_path in (*PRIVATE_PATHS, *PUBLIC_PATHS):
            path = root / f"{prefix}{relative_path}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic fixture\n", encoding="utf-8")
        public_members.update(f"{prefix}{name}" for name in PUBLIC_PATHS)
    return public_members


@pytest.mark.parametrize("relative_path", PRIVATE_PATHS)
def test_shared_predicate_rejects_private_members_at_any_depth(relative_path: str) -> None:
    for prefix in ("", "Pandrator/", "package/nested/"):
        name = f"{prefix}{relative_path}"
        assert auditor.is_private_archive_member(name)
        assert auditor.is_private_archive_member(name.upper().replace("/", "\\"))
        with pytest.raises(ValueError, match="rejected archive members"):
            auditor.validate_members(Path("fixture.whl"), [name])


@pytest.mark.parametrize("name", ("/absolute/code.py", "../code.py", "a/../code.py"))
def test_shared_predicate_preserves_unsafe_path_rejection(name: str) -> None:
    assert auditor.is_private_archive_member(name)
    assert builder.should_exclude_path(name)


def test_copy_and_emitted_zip_contain_only_public_members(tmp_path: Path) -> None:
    # Absolute scratch ancestors must not participate in member filtering.
    source = tmp_path / ".local-notes" / "source"
    expected_members = populate_tree(source)
    stage = tmp_path / "stage"
    builder.copy_relative_entries(source, stage, ["."], prefer_hardlinks=False)
    assert {path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()} == (
        expected_members
    )
    archive_path = tmp_path / "copied.zip"
    builder.create_zip_archive(stage, archive_path)
    with ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == expected_members
        auditor.validate_members(archive_path, archive.namelist())


def test_final_zip_filters_polluted_prepared_tree(tmp_path: Path) -> None:
    prepared = tmp_path / ".local-notes" / "prepared"
    expected_members = populate_tree(prepared)
    archive_path = tmp_path / "prepared.zip"
    builder.create_zip_archive(prepared, archive_path)
    with ZipFile(archive_path) as archive:
        assert set(archive.namelist()) == expected_members


def test_explicit_private_includes_are_skipped(tmp_path: Path) -> None:
    source = tmp_path / "source"
    populate_tree(source)
    stage = tmp_path / "stage"
    private_includes = [f"Pandrator/{name}" for name in PRIVATE_PATHS]
    private_includes += ["Pandrator/.local-notes", "Pandrator/docs/plans", "review-notes"]
    builder.copy_relative_entries(
        source, stage, [*private_includes, "Pandrator/code.py"], prefer_hardlinks=True
    )
    assert [path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()] == [
        "Pandrator/code.py"
    ]
    assert (stage / "Pandrator/code.py").read_bytes() == (source / "Pandrator/code.py").read_bytes()


def test_private_changes_leave_block_signature_unchanged(tmp_path: Path) -> None:
    source = tmp_path / "source"
    populate_tree(source)
    includes = ["Pandrator", "Pandrator/docs/plans/local-plan.md", "missing/.env"]
    before = builder.calculate_block_signature(source, includes)
    for name in PRIVATE_PATHS:
        (source / "Pandrator" / name).write_text("changed synthetic private fixture\n")
    (source / "Pandrator/reviews/added.txt").write_text("synthetic new private fixture\n")
    (source / "missing").mkdir()
    (source / "missing/.env").write_text("synthetic environment fixture\n")
    assert builder.calculate_block_signature(source, includes) == before
    (source / "Pandrator/code.py").write_text("changed synthetic public fixture\n")
    assert builder.calculate_block_signature(source, includes) != before


def test_custom_basename_exclusions_preserve_mandatory_privacy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    populate_tree(source)
    for name in (
        "Pandrator/omit-file.txt",
        "Pandrator/omit-directory/code.py",
        "Pandrator/run.log",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic excluded fixture\n")
    with patch.object(builder, "ACTIVE_EXCLUDED_FILE_PREFIXES", ("omit-",)):
        stage = tmp_path / "stage"
        builder.copy_relative_entries(source, stage, ["Pandrator"], prefer_hardlinks=False)
        assert {
            path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()
        } == {f"Pandrator/{name}" for name in PUBLIC_PATHS}
    assert builder.should_exclude_path("package/custom-directory/code.py", ("custom-",))
    assert builder.should_exclude_path("package/.env.example", ("custom-",))
    assert builder.should_exclude_path("package/docs/plans/result.json", ("custom-",))
    assert not builder.should_exclude_path("package/config.json", ("custom-",))


def test_walk_prunes_private_directories_before_descent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    populate_tree(source)
    original_scandir = builder.os.scandir
    visited = []

    def record_scandir(path):
        visited.append(Path(path).relative_to(source).as_posix())
        return original_scandir(path)

    with patch.object(builder.os, "scandir", side_effect=record_scandir):
        list(builder.iter_files_in_tree(source, relative_prefix=""))
    assert "Pandrator/docs/guides" in visited
    assert not any(auditor.is_private_archive_member(path) for path in visited)


def test_direct_copy_preserves_docs_context(tmp_path: Path) -> None:
    source = tmp_path / "source"
    populate_tree(source)
    stage = tmp_path / "stage"
    builder.copy_directory_contents(source / "docs", stage, prefer_hardlinks=False)
    assert [path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file()] == [
        "guides/guide.md"
    ]
