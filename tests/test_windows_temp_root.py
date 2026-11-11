import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from tests import windows_temp_root as wtr


class WindowsTempRootTests(unittest.TestCase):
    """The helper on any OS: a temporary folder stands in for the temp drive's root."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.drive = Path(tmp.name)
        self.temp_dir = self.drive / "Temp"
        self.root = self.drive.joinpath(*wtr.ROOT_PARTS)
        self.restricted: list[Path] = []
        self.live: set[str] = set()
        self.real_short_root_path = wtr.short_root_path
        drive = self.drive
        patcher = patch.object(
            wtr, "short_root_path", lambda temp_dir: drive.joinpath(*wtr.ROOT_PARTS)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def finished(self, lock: Path) -> bool:
        # Stands in for the Windows sharing check, which can't tell runs apart in one process.
        return lock.name.removesuffix(wtr.LOCK_SUFFIX) not in self.live

    def run_dir(self, **kwargs):
        kwargs.setdefault("platform", "win32")
        kwargs.setdefault("temp_dir", self.temp_dir)
        kwargs.setdefault("environ", {})
        kwargs.setdefault("restrict", self.restricted.append)
        kwargs.setdefault("is_finished", self.finished)
        run = wtr.make_run_dir(**kwargs)
        if run is not None:
            self.addCleanup(os.close, run.lock_fd)
        return run

    def make_run_folder(self, name: str, mtime: float) -> Path:
        folder = self.root / name
        folder.mkdir()
        os.utime(folder, (mtime, mtime))
        return folder

    def test_windows_without_basetemp_gets_a_short_locked_run_in_a_restricted_root(self):
        run = self.run_dir()
        self.assertEqual(run.path.parent, self.root)
        self.assertEqual(len(run.path.name), wtr.NAME_LENGTH)
        self.assertEqual((self.root / wtr.MARKER_NAME).read_text(encoding="utf-8"), wtr.MARKER_TEXT)
        self.assertEqual(self.restricted, [self.root])
        self.assertTrue((self.root / f"{run.path.name}{wtr.LOCK_SUFFIX}").is_file())
        self.assertFalse(run.path.exists(), "pytest creates the folder on first use")
        second = self.run_dir()
        self.assertNotEqual(run.path, second.path)
        self.assertEqual(self.restricted, [self.root], "a marked root is restricted once")

    def test_explicit_basetemp_is_untouched(self):
        self.assertIsNone(self.run_dir(explicit_basetemp=Path("x")))
        self.assertFalse(self.root.exists())

    def test_pytest_debug_temproot_is_untouched(self):
        self.assertIsNone(self.run_dir(environ={"PYTEST_DEBUG_TEMPROOT": "D:\\t"}))
        self.assertFalse(self.root.exists())

    def test_other_platform_is_untouched(self):
        self.assertIsNone(self.run_dir(platform="linux"))
        self.assertFalse(self.root.exists())

    def test_keeps_newest_finished_runs_and_every_live_one(self):
        self.run_dir()
        for index, name in enumerate(("aaaa", "bbbb", "cccc", "dddd", "eeee")):
            self.make_run_folder(name, 1_000_000.0 + index * 60)
        live = self.make_run_folder("live", 1.0)
        (self.root / f"live{wtr.LOCK_SUFFIX}").write_text("")
        self.live.add("live")
        stray = self.root / "notes.txt"
        stray.write_text("not a run")
        own = self.run_dir(keep=2)
        remaining = {child.name for child in self.root.iterdir() if child.is_dir()}
        self.assertEqual(remaining, {"dddd", "eeee", "live"})
        self.assertTrue(live.exists())
        self.assertTrue(stray.exists())
        self.assertTrue((self.root / wtr.MARKER_NAME).exists())
        self.assertTrue((self.root / f"{own.path.name}{wtr.LOCK_SUFFIX}").exists())

    def test_zero_keep_removes_every_finished_run(self):
        self.run_dir()
        self.make_run_folder("aaaa", 1_000_000.0)
        self.run_dir(keep=0)
        self.assertFalse((self.root / "aaaa").exists())

    def test_read_only_files_in_a_finished_run_are_removed(self):
        self.run_dir()
        folder = self.make_run_folder("aaaa", 1_000_000.0)
        objects = folder / "repo" / ".git" / "objects"
        objects.mkdir(parents=True)
        blob = objects / "ab12"
        blob.write_text("git object")
        blob.chmod(stat.S_IREAD)
        self.run_dir(keep=0)
        self.assertFalse(folder.exists())

    def test_foreign_folder_is_left_alone(self):
        self.root.mkdir(parents=True)
        (self.root / "keep").mkdir()
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "--basetemp"):
            self.run_dir()
        self.assertTrue((self.root / "keep").exists())
        self.assertEqual(self.restricted, [])

    def test_stray_file_makes_an_unmarked_root_foreign(self):
        self.root.mkdir(parents=True)
        (self.root / "x.txt").write_text("someone else's")
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.run_dir()
        self.assertFalse((self.root / wtr.MARKER_NAME).exists())

    def test_root_marked_by_a_racing_lane_is_accepted(self):
        self.root.mkdir(parents=True)
        (self.root / f"abcd{wtr.LOCK_SUFFIX}").write_text("")
        with patch.object(wtr, "_read_marker", side_effect=[None, wtr.MARKER_TEXT]):
            run = self.run_dir()
        self.assertEqual(run.path.parent, self.root)
        self.assertEqual(self.restricted, [])

    def test_empty_unmarked_root_is_adopted_restricted_and_marked(self):
        self.root.mkdir(parents=True)
        run = self.run_dir()
        self.assertEqual(run.path.parent, self.root)
        self.assertEqual(self.restricted, [self.root])
        self.assertEqual((self.root / wtr.MARKER_NAME).read_text(encoding="utf-8"), wtr.MARKER_TEXT)

    def test_older_marker_is_restricted_and_rewritten(self):
        self.root.mkdir(parents=True)
        (self.root / wtr.MARKER_NAME).write_text(
            "Temp roots for pytest runs; stale runs are pruned.\n"
        )
        (self.root / "k3j9x0a1").mkdir()
        self.run_dir()
        self.assertEqual(self.restricted, [self.root])
        self.assertEqual((self.root / wtr.MARKER_NAME).read_text(encoding="utf-8"), wtr.MARKER_TEXT)

    def test_failed_restriction_falls_back_without_marking(self):
        def refuse(root):
            raise wtr.ShortRootUnavailable("icacls refused")

        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "icacls refused"):
            self.run_dir(restrict=refuse)
        self.assertFalse((self.root / wtr.MARKER_NAME).exists())

    def test_uncreatable_root_raises(self):
        blocker = self.drive / "blocker"
        blocker.write_text("a file, so nothing can be created beneath it")
        with patch.object(wtr, "short_root_path", lambda temp_dir: blocker / "pt"):
            with self.assertRaises(wtr.ShortRootUnavailable):
                self.run_dir()

    def test_unlockable_root_raises(self):
        with patch.object(wtr.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "cannot lock"):
                self.run_dir()

    def test_missing_temp_directory_raises(self):
        with patch.object(wtr.tempfile, "gettempdir", side_effect=FileNotFoundError("none")):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "no usable temp directory"):
                self.run_dir(temp_dir=None)

    def test_real_short_root_path(self):
        absolute = self.temp_dir.resolve()
        self.assertEqual(self.real_short_root_path(absolute), Path(absolute.anchor, "tmp", "pt"))
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.real_short_root_path(Path("relative") / "temp")
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "network share"):
            self.real_short_root_path("\\\\server\\share\\Temp")


@unittest.skipUnless(sys.platform == "win32", "Windows file sharing and ACL behaviour")
class WindowsOnlyTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def test_held_lock_is_live_until_closed(self):
        lock = self.folder / f"abcd{wtr.LOCK_SUFFIX}"
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            self.assertFalse(wtr.release_finished_lock(lock))
            self.assertTrue(lock.exists())
        finally:
            os.close(fd)
        self.assertTrue(wtr.release_finished_lock(lock))
        self.assertFalse(lock.exists())

    def test_restrict_leaves_only_the_current_user(self):
        root = self.folder / "pt"
        root.mkdir()
        wtr.restrict_to_current_user(root)
        listing = subprocess.run(
            ["icacls", str(root)], capture_output=True, text=True, check=True
        ).stdout
        entries = [line for line in listing.splitlines() if ":(" in line]
        self.assertEqual(len(entries), 1, listing)
        self.assertIn(os.environ["USERNAME"].lower(), entries[0].lower())
        self.assertIn("(OI)(CI)(F)", entries[0])

    def test_restrict_removes_explicit_entries_too(self):
        # GitHub's Windows runners give new folders explicit entries, which
        # /inheritance:r alone leaves in place. Make them explicit here too.
        root = self.folder / "pt"
        root.mkdir()
        subprocess.run(["icacls", str(root), "/inheritance:d"], capture_output=True, check=True)
        wtr.restrict_to_current_user(root)
        listing = subprocess.run(
            ["icacls", str(root)], capture_output=True, text=True, check=True
        ).stdout
        entries = [line for line in listing.splitlines() if ":(" in line]
        self.assertEqual(len(entries), 1, listing)
        self.assertIn(os.environ["USERNAME"].lower(), entries[0].lower())


@pytest.mark.skipif(sys.platform != "win32", reason="the short root applies only on Windows")
def test_session_basetemp_is_under_the_short_root(request, tmp_path_factory):
    given = " ".join(map(str, request.config.invocation_params.args))
    given += " " + os.environ.get("PYTEST_ADDOPTS", "")
    if "--basetemp" in given or os.environ.get("PYTEST_DEBUG_TEMPROOT"):
        pytest.skip("an explicit temp root was given")
    root = wtr.short_root_path(tempfile.gettempdir()).resolve()
    # Under xdist a worker's basetemp is <run>/popen-gwN, so check ancestry.
    assert root in tmp_path_factory.getbasetemp().parents


class PytestConfigureHookTests(unittest.TestCase):
    @staticmethod
    def config(basetemp=None, retention="3"):
        return SimpleNamespace(
            option=SimpleNamespace(basetemp=basetemp),
            issue_config_time_warning=MagicMock(),
            getini=MagicMock(return_value=retention),
            add_cleanup=MagicMock(),
        )

    def test_sets_basetemp_and_releases_the_lock_at_cleanup(self):
        from tests import conftest

        handle, lock = tempfile.mkstemp()
        self.addCleanup(os.unlink, lock)
        config = self.config()
        run = wtr.RunDir(Path("C:/tmp/pt/abcd"), handle)
        with patch.object(conftest, "make_run_dir", return_value=run) as make:
            conftest.pytest_configure(config)
        make.assert_called_once_with(explicit_basetemp=None, keep=3)
        self.assertEqual(config.option.basetemp, Path("C:/tmp/pt/abcd"))
        config.issue_config_time_warning.assert_not_called()
        config.add_cleanup.assert_called_once()
        config.add_cleanup.call_args.args[0]()
        with self.assertRaises(OSError):
            os.close(handle)

    def test_explicit_basetemp_passes_through_the_real_helper(self):
        from tests import conftest

        given = Path("D:/given")
        config = self.config(basetemp=given)
        with (
            patch.object(wtr, "sys", SimpleNamespace(platform="win32")),
            patch.object(conftest, "make_run_dir", wraps=wtr.make_run_dir) as make,
        ):
            conftest.pytest_configure(config)
        make.assert_called_once_with(explicit_basetemp=given, keep=3)
        self.assertEqual(config.option.basetemp, given)
        config.issue_config_time_warning.assert_not_called()
        config.add_cleanup.assert_not_called()

    def test_unreadable_retention_count_uses_the_default(self):
        from tests import conftest

        config = self.config(retention="lots")
        with patch.object(conftest, "make_run_dir", return_value=None) as make:
            conftest.pytest_configure(config)
        make.assert_called_once_with(explicit_basetemp=None, keep=wtr.DEFAULT_KEEP)

    def test_warns_and_leaves_basetemp_when_root_unavailable(self):
        from tests import conftest

        config = self.config()
        with patch.object(conftest, "make_run_dir", side_effect=wtr.ShortRootUnavailable("no")):
            conftest.pytest_configure(config)
        self.assertIsNone(config.option.basetemp)
        config.issue_config_time_warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
