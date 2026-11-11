import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
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
        if lock.name.removesuffix(wtr.LOCK_SUFFIX) in self.live:
            return False
        lock.unlink(missing_ok=True)
        return True

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

    def make_run_folder(self, name: str, mtime: float, *, lock: bool = True) -> Path:
        folder = self.root / name
        folder.mkdir()
        os.utime(folder, (mtime, mtime))
        if lock:
            (self.root / f"{name}{wtr.LOCK_SUFFIX}").write_text("")
            (self.root / f"{name}{wtr.RUN_MARKER_SUFFIX}").write_text(wtr.RUN_MARKER_TEXT)
        return folder

    def marker_path(self) -> Path:
        return self.root / wtr.MARKER_NAME

    def test_windows_without_basetemp_gets_a_short_locked_run_in_a_restricted_root(self):
        run = self.run_dir()
        self.assertEqual(run.path.parent, self.root)
        self.assertEqual(len(run.path.name), wtr.NAME_LENGTH)
        self.assertTrue(wtr.RUN_NAME_PATTERN.fullmatch(run.path.name))
        self.assertEqual(self.marker_path().read_text(encoding="utf-8"), wtr.MARKER_TEXT)
        self.assertEqual(self.restricted, [self.root])
        self.assertTrue((self.root / f"{run.path.name}{wtr.LOCK_SUFFIX}").is_file())
        self.assertTrue((self.root / wtr.ROOT_LOCK_NAME).is_file())
        self.assertFalse((self.root / wtr.MARKER_TEMP_NAME).exists())
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

    def test_keeps_newest_finished_owned_runs_and_every_live_one(self):
        self.run_dir()
        for index, name in enumerate(("aaaa", "bbbb", "cccc", "dddd", "eeee")):
            self.make_run_folder(name, 1_000_000.0 + index * 60)
        live = self.make_run_folder("live", 1.0)
        self.live.add("live")
        stray = self.root / "notes.txt"
        stray.write_text("not a run")
        # Neither of these proves ownership, so neither may be pruned.
        unrelated = self.root / "personal_notes"
        unrelated.mkdir()
        (unrelated / "2026-plans.txt").write_text("mine")
        unlocked = self.make_run_folder("zzzz", 1.0, lock=False)
        own = self.run_dir(keep=2)
        remaining = {child.name for child in self.root.iterdir() if child.is_dir()}
        self.assertEqual(remaining, {"dddd", "eeee", "live", "personal_notes", "zzzz"}, remaining)
        self.assertTrue(live.exists())
        self.assertTrue(stray.exists())
        self.assertTrue((unrelated / "2026-plans.txt").exists())
        self.assertTrue(unlocked.exists())
        self.assertTrue(self.marker_path().exists())
        self.assertTrue((self.root / f"{own.path.name}{wtr.LOCK_SUFFIX}").exists())

    def test_retained_finished_run_keeps_its_ownership_for_future_pruning(self):
        self.run_dir()
        self.make_run_folder("aaaa", 1_000_000.0)
        self.make_run_folder("bbbb", 1_000_100.0)
        self.make_run_folder("cccc", 1_000_200.0)
        self.run_dir(keep=2)
        self.assertFalse((self.root / "aaaa").exists())
        self.assertTrue((self.root / f"bbbb{wtr.RUN_MARKER_SUFFIX}").exists())
        self.assertTrue((self.root / f"cccc{wtr.RUN_MARKER_SUFFIX}").exists())

        self.make_run_folder("dddd", 1_000_300.0)
        self.run_dir(keep=2)
        self.assertFalse((self.root / "bbbb").exists())
        self.assertFalse((self.root / f"bbbb{wtr.RUN_MARKER_SUFFIX}").exists())
        self.assertTrue((self.root / "cccc").exists())
        self.assertTrue((self.root / "dddd").exists())

    def test_forged_lock_without_ownership_marker_does_not_authorize_deletion(self):
        self.run_dir()
        foreign = self.make_run_folder("abcd", 1_000_000.0)
        (self.root / f"abcd{wtr.RUN_MARKER_SUFFIX}").unlink()
        self.run_dir(keep=0)
        self.assertTrue(foreign.exists())

    def test_zero_keep_removes_every_finished_run(self):
        self.run_dir()
        self.make_run_folder("aaaa", 1_000_000.0)
        self.run_dir(keep=0)
        self.assertFalse((self.root / "aaaa").exists())
        self.assertFalse((self.root / f"aaaa{wtr.LOCK_SUFFIX}").exists())

    def test_keep_all_removes_nothing(self):
        self.run_dir()
        for name in ("aaaa", "bbbb", "cccc", "dddd"):
            self.make_run_folder(name, 1_000_000.0)
        self.run_dir(keep=None)
        self.assertEqual(
            {child.name for child in self.root.iterdir() if child.is_dir()},
            {"aaaa", "bbbb", "cccc", "dddd"},
        )

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

    def test_run_folder_without_a_lock_is_never_deleted(self):
        self.run_dir()
        unlocked = self.make_run_folder("abcd", 1_000_000.0, lock=False)
        self.run_dir(keep=0)
        self.assertTrue(unlocked.exists())

    def test_finished_lock_without_a_folder_is_removed_but_a_live_one_is_kept(self):
        self.run_dir()
        finished = self.root / f"aaaa{wtr.LOCK_SUFFIX}"
        finished.write_text("")
        (self.root / f"aaaa{wtr.RUN_MARKER_SUFFIX}").write_text(wtr.RUN_MARKER_TEXT)
        live = self.root / f"bbbb{wtr.LOCK_SUFFIX}"
        live.write_text("")
        (self.root / f"bbbb{wtr.RUN_MARKER_SUFFIX}").write_text(wtr.RUN_MARKER_TEXT)
        self.live.add("bbbb")
        self.run_dir(keep=0)
        self.assertFalse(finished.exists(), "a finished run that never used tmp_path")
        self.assertTrue(live.exists(), "its session may still create the folder")

    def test_live_lock_without_a_folder_is_left_alone(self):
        self.run_dir()
        lock = self.root / f"abcd{wtr.LOCK_SUFFIX}"
        lock.write_text("")
        (self.root / f"abcd{wtr.RUN_MARKER_SUFFIX}").write_text(wtr.RUN_MARKER_TEXT)
        self.live.add("abcd")
        self.run_dir(keep=0)
        self.assertTrue(lock.exists(), "a run may hold its lock before creating its folder")

    def test_lock_for_a_non_run_name_is_ignored(self):
        self.run_dir()
        folder = self.make_run_folder("notes", 1_000_000.0)
        self.run_dir(keep=0)
        self.assertTrue(folder.exists())

    def test_symlinked_run_folder_is_not_followed(self):
        self.run_dir()
        outside = self.drive / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not ours")
        (self.root / f"abcd{wtr.LOCK_SUFFIX}").write_text("")
        try:
            (self.root / "abcd").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this host cannot create directory symlinks")
        self.run_dir(keep=0)
        self.assertTrue((outside / "keep.txt").exists())

    def test_pruning_is_refused_on_an_unmarked_root(self):
        self.root.mkdir(parents=True)
        folder = self.make_run_folder("aaaa", 1_000_000.0)
        wtr.prune_runs(self.root, 0, "own", is_finished=self.finished)
        self.assertTrue(folder.exists())
        self.assertTrue((self.root / f"aaaa{wtr.LOCK_SUFFIX}").exists())

    def test_another_checkouts_finished_run_is_pruned_but_its_checkout_folder_survives(self):
        self.run_dir()
        other_run = self.make_run_folder("qqqq", 1_000_000.0)
        checkout = self.root / "my-other-checkout"
        checkout.mkdir()
        (checkout / "pyproject.toml").write_text("[project]\n")
        notes = self.root / "personal_notes"
        notes.mkdir()
        self.run_dir(keep=0)
        self.assertFalse(other_run.exists())
        self.assertTrue((checkout / "pyproject.toml").exists())
        self.assertTrue(notes.exists())

    def test_acl_verifier_refuses_foreign_rules_without_rewriting_them(self):
        self.root.mkdir(parents=True)
        with (
            patch.object(wtr.sys, "platform", "win32"),
            patch.object(
                wtr.subprocess,
                "run",
                return_value=SimpleNamespace(returncode=3, stdout="", stderr="denied"),
            ) as powershell,
        ):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "unverified ownership"):
                wtr._verify_private_root(self.root)
        command = powershell.call_args.args[0]
        self.assertEqual(command[:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])
        self.assertEqual(
            powershell.call_args.kwargs["env"]["PANDRATOR_PYTEST_ACL_ROOT"], str(self.root)
        )

    @unittest.skipUnless(sys.platform == "win32", "requires actual Windows ACLs")
    def test_real_acl_validation_accepts_a_fresh_restricted_root(self):
        wtr.prepare_root(self.root)
        wtr.prepare_root(self.root)
        wtr._verify_private_root(self.root)

    def test_foreign_junction_or_symlink_root_is_refused_before_acl_change(self):
        outside = self.drive / "outside"
        outside.mkdir()
        (outside / "notes").write_text("leave this alone")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.root.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable")
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "symlink or junction"):
            self.run_dir()
        self.assertEqual(self.restricted, [])
        self.assertEqual((outside / "notes").read_text(), "leave this alone")

    def test_existing_valid_marker_requires_private_acl_verification_on_windows(self):
        self.run_dir()
        # An attacker can write the public marker text. Windows must verify
        # the real owner and DACL even if the text matches.
        with (
            patch.object(wtr.sys, "platform", "win32"),
            patch.object(
                wtr, "_verify_private_root", side_effect=wtr.ShortRootUnavailable("bad ACL")
            ) as verify,
            patch.object(wtr, "restrict_to_current_user", self.restricted.append),
        ):
            # Invoke default function explicitly, rather than our fake restrict,
            # so the real policy's ACL-check branch is exercised.
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "bad ACL"):
                wtr.prepare_root(self.root, wtr.restrict_to_current_user)
            self.assertEqual(verify.call_count, 1)

    def test_foreign_folder_is_left_alone(self):
        self.root.mkdir(parents=True)
        foreign = self.root / "personal_notes"
        foreign.mkdir()
        (foreign / "diary.txt").write_text("do not delete")
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "--basetemp"):
            self.run_dir()
        self.assertTrue((foreign / "diary.txt").exists())
        self.assertEqual(self.restricted, [])
        self.assertFalse(self.marker_path().exists())
        self.assertFalse((self.root / wtr.ROOT_LOCK_NAME).exists(), "no file may be created")

    def test_stray_file_makes_an_unmarked_root_foreign(self):
        self.root.mkdir(parents=True)
        (self.root / "x.txt").write_text("someone else's")
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.run_dir()
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.restricted, [])

    def test_truncated_marker_is_refused_without_touching_acls_or_the_marker(self):
        self.root.mkdir(parents=True)
        truncated = wtr.MARKER_TEXT[:-12]
        self.marker_path().write_text(truncated, encoding="utf-8")
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "not made by these tests"):
            self.run_dir()
        self.assertEqual(self.marker_path().read_text(encoding="utf-8"), truncated)
        self.assertEqual(self.restricted, [])
        self.assertFalse((self.root / wtr.ROOT_LOCK_NAME).exists())

    def test_unknown_marker_is_refused(self):
        self.root.mkdir(parents=True)
        self.marker_path().write_text("some other tool's root\n", encoding="utf-8")
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.run_dir()
        self.assertEqual(self.marker_path().read_text(encoding="utf-8"), "some other tool's root\n")
        self.assertEqual(self.restricted, [])

    def test_undecodable_marker_is_refused(self):
        self.root.mkdir(parents=True)
        self.marker_path().write_bytes(b"\xff\xfe\x00not utf-8")
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.run_dir()
        self.assertEqual(self.marker_path().read_bytes(), b"\xff\xfe\x00not utf-8")
        self.assertEqual(self.restricted, [])

    def test_existing_empty_unmarked_root_is_not_adopted(self):
        self.root.mkdir(parents=True)
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "not made by these tests"):
            self.run_dir()
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.restricted, [])

    def test_crashed_unmarked_root_fails_closed(self):
        self.root.mkdir(parents=True)
        (self.root / wtr.MARKER_TEMP_NAME).write_text(wtr.MARKER_TEXT[:5])
        (self.root / wtr.ROOT_LOCK_NAME).write_text("0")
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.run_dir()
        self.assertFalse(self.marker_path().exists())
        self.assertEqual(self.restricted, [])

    def test_concurrent_starts_adopt_once_and_publish_a_complete_marker(self):
        calls: list[Path] = []

        def slow_restrict(root: Path) -> None:
            calls.append(root)
            time.sleep(0.15)

        def start() -> wtr.RunDir:
            run = wtr.make_run_dir(
                platform="win32",
                temp_dir=self.temp_dir,
                environ={},
                restrict=slow_restrict,
                # Preparation is under test; locking liveness is simulated elsewhere.
                is_finished=lambda lock: False,
            )
            assert run is not None
            return run

        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = list(pool.map(lambda _: start(), range(2)))
        try:
            self.assertEqual(len(calls), 1, "only one lane may reset ACLs")
            self.assertEqual(self.marker_path().read_text(encoding="utf-8"), wtr.MARKER_TEXT)
            self.assertFalse((self.root / wtr.MARKER_TEMP_NAME).exists())
            self.assertNotEqual(runs[0].path, runs[1].path)
            for run in runs:
                self.assertTrue((self.root / f"{run.path.name}{wtr.LOCK_SUFFIX}").is_file())
        finally:
            for run in runs:
                os.close(run.lock_fd)

    def test_marker_write_failure_leaves_no_marker_and_no_temp_file(self):
        with patch.object(wtr.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "cannot prepare"):
                self.run_dir()
        self.assertFalse(self.marker_path().exists())
        self.assertFalse((self.root / wtr.MARKER_TEMP_NAME).exists())
        # The lane that created the failed root removes it. A later lane
        # can initialize cleanly without anyone deleting shared data by hand.
        self.assertFalse(self.root.exists())
        run = self.run_dir()
        self.assertEqual(run.path.parent, self.root)
        self.assertEqual(self.marker_path().read_text(encoding="utf-8"), wtr.MARKER_TEXT)

    def test_failed_new_root_permission_setup_does_not_poison_the_next_run(self):
        def reject(_root: Path) -> None:
            raise wtr.ShortRootUnavailable("bad ACL")

        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "bad ACL"):
            wtr.prepare_root(self.root, restrict=reject)
        self.assertFalse(self.root.exists())
        run = self.run_dir()
        self.assertEqual(run.path.parent, self.root)

    @unittest.skipUnless(sys.platform == "win32", "requires Windows file locks")
    def test_failed_real_windows_acl_verification_cleans_up_created_root(self):
        with patch.object(
            wtr, "_verify_private_root", side_effect=wtr.ShortRootUnavailable("bad ACL")
        ):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "bad ACL"):
                wtr.prepare_root(self.root)
        self.assertFalse(self.root.exists())

    def test_failed_restriction_falls_back_without_marking(self):
        def refuse(root):
            raise wtr.ShortRootUnavailable("icacls refused")

        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "icacls refused"):
            self.run_dir(restrict=refuse)
        self.assertFalse(self.marker_path().exists())
        self.assertFalse(self.root.exists(), "a failed new root must not persist")

    def test_uncreatable_root_raises(self):
        blocker = self.drive / "blocker"
        blocker.write_text("a file, so nothing can be created beneath it")
        with patch.object(wtr, "short_root_path", lambda temp_dir: blocker / "pt"):
            with self.assertRaises(wtr.ShortRootUnavailable):
                self.run_dir()

    def test_unwritable_c_drive_falls_back_instead_of_crashing(self):
        # A read-only drive refuses even the root lock file.
        with patch.object(wtr.os, "open", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "cannot prepare"):
                self.run_dir()

    def test_root_owned_by_another_user_falls_back_with_a_clear_message(self):
        self.run_dir()  # a marked root; now it behaves like another user's folder
        # The marker is readable, but the folder refuses new files.
        with patch.object(wtr.os, "open", side_effect=PermissionError("Access is denied")):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "cannot lock a run"):
                self.run_dir()

    def test_missing_temp_directory_raises(self):
        with patch.object(wtr.tempfile, "gettempdir", side_effect=FileNotFoundError("none")):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "no usable temp directory"):
                self.run_dir(temp_dir=None)

    def test_root_lock_timeout_is_reported(self):
        with (
            patch.object(wtr, "BOOTSTRAP_LOCK_TIMEOUT", 0.0),
            patch.object(wtr, "_lock_fd", side_effect=OSError(wtr.errno.EACCES, "busy")),
        ):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "timed out"):
                self.run_dir()
        self.assertEqual(self.restricted, [])

    def test_real_short_root_path(self):
        absolute = self.temp_dir.resolve()
        self.assertEqual(self.real_short_root_path(absolute), Path(absolute.anchor, "tmp", "pt"))
        with self.assertRaises(wtr.ShortRootUnavailable):
            self.real_short_root_path(Path("relative") / "temp")
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "network share"):
            self.real_short_root_path("\\\\server\\share\\Temp")


class RestrictionTests(unittest.TestCase):
    """icacls interaction, with the tool itself mocked so this runs on Linux too."""

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "pt"
        self.root.mkdir()
        patch.dict(os.environ, {"USERNAME": "luke", "USERDOMAIN": "PC"}, clear=True).start()
        self.addCleanup(patch.stopall)

    def icacls(self, *results: object):
        runner = patch.object(wtr.subprocess, "run")
        run = runner.start()
        run.side_effect = list(results)
        return run

    @staticmethod
    def result(code: int = 0, output: str = "") -> SimpleNamespace:
        return SimpleNamespace(returncode=code, stdout=output, stderr="")

    def test_restriction_resets_then_grants_the_current_user_only(self):
        run = self.icacls(self.result(), self.result(), self.result())
        wtr.restrict_to_current_user(self.root)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["icacls", str(self.root), "/reset"],
                [
                    "icacls",
                    str(self.root),
                    "/inheritance:r",
                    "/grant:r",
                    "PC\\luke:(OI)(CI)F",
                ],
                ["icacls", str(self.root), "/setowner", "PC\\luke"],
            ],
        )
        self.assertEqual(
            run.call_args_list[0].kwargs, {"capture_output": True, "text": True, "check": False}
        )

    def test_failed_grant_restores_inherited_access(self):
        run = self.icacls(
            self.result(),
            self.result(5, "Access is denied."),
            self.result(),
            self.result(),
        )
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "another user may own it"):
            wtr.restrict_to_current_user(self.root)
        rollback = [call.args[0] for call in run.call_args_list[2:]]
        self.assertEqual(rollback[0], ["icacls", str(self.root), "/inheritance:e"])
        self.assertEqual(rollback[1], ["icacls", str(self.root), "/grant:r", "PC\\luke:F"])

    def test_failed_owner_change_rolls_back_inherited_access(self):
        run = self.icacls(
            self.result(),
            self.result(),
            self.result(5, "Access is denied."),
            self.result(),
            self.result(),
        )
        with self.assertRaises(wtr.ShortRootUnavailable):
            wtr.restrict_to_current_user(self.root)
        self.assertEqual(
            [call.args[0] for call in run.call_args_list[3:]],
            [
                ["icacls", str(self.root), "/inheritance:e"],
                ["icacls", str(self.root), "/grant:r", "PC\\luke:F"],
            ],
        )

    def test_failed_reset_touches_nothing_else(self):
        run = self.icacls(self.result(5, "Access is denied."))
        with self.assertRaises(wtr.ShortRootUnavailable):
            wtr.restrict_to_current_user(self.root)
        self.assertEqual(len(run.call_args_list), 1)

    def test_successful_rollback_failure_is_suppressed(self):
        self.icacls(
            self.result(),
            self.result(5),
            self.result(5),
            self.result(5),
        )
        with self.assertRaises(wtr.ShortRootUnavailable):
            wtr.restrict_to_current_user(self.root)

    def test_missing_icacls_is_reported(self):
        run = self.icacls(FileNotFoundError("icacls not found"))
        with self.assertRaisesRegex(wtr.ShortRootUnavailable, "cannot run icacls"):
            wtr.restrict_to_current_user(self.root)
        self.assertEqual(len(run.call_args_list), 1)

    def test_missing_username_is_reported(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(wtr.ShortRootUnavailable, "USERNAME"):
                wtr.restrict_to_current_user(self.root)

    def test_local_account_without_a_domain_is_granted_by_name(self):
        with patch.dict(os.environ, {"USERNAME": "luke"}, clear=True):
            run = self.icacls(self.result(), self.result(), self.result())
            wtr.restrict_to_current_user(self.root)
        self.assertEqual(run.call_args_list[1].args[0][-1], "luke:(OI)(CI)F")


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

    def test_lock_held_by_another_process_is_live(self):
        lock = self.folder / f"abcd{wtr.LOCK_SUFFIX}"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,sys,time;"
                "fd=os.open(sys.argv[1], os.O_CREAT|os.O_RDWR);"
                "os.write(fd,b'0');"
                "print('ready',flush=True);"
                "time.sleep(60)",
                str(lock),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            self.assertFalse(wtr.release_finished_lock(lock))
            self.assertTrue(lock.exists())
        finally:
            process.terminate()
            process.wait(timeout=30)
            process.stdout.close()

    def test_two_processes_prepare_the_root_once(self):
        root = self.folder / "pt"
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]);"
            "from pathlib import Path;"
            "from tests import windows_temp_root as wtr;"
            "wtr.prepare_root(Path(sys.argv[2]))"
        )
        repo_root = Path(__file__).resolve().parents[1]
        processes = [
            subprocess.Popen([sys.executable, "-c", code, str(repo_root), str(root)])
            for _ in range(2)
        ]
        try:
            self.assertEqual([process.wait(timeout=120) for process in processes], [0, 0])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
        self.assertEqual((root / wtr.MARKER_NAME).read_text(encoding="utf-8"), wtr.MARKER_TEXT)

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
    def config(basetemp=None, retention="3", policy="all"):
        ini = {
            "tmp_path_retention_count": retention,
            "tmp_path_retention_policy": policy,
        }
        return SimpleNamespace(
            option=SimpleNamespace(basetemp=basetemp),
            issue_config_time_warning=MagicMock(),
            getini=MagicMock(side_effect=lambda name: ini[name]),
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

    def test_retention_count_is_honoured(self):
        from tests import conftest

        config = self.config(retention="7", policy="failed")
        with patch.object(conftest, "make_run_dir", return_value=None) as make:
            conftest.pytest_configure(config)
        make.assert_called_once_with(explicit_basetemp=None, keep=7)

    def test_retention_policy_none_keeps_no_finished_runs(self):
        from tests import conftest

        config = self.config(retention="7", policy="none")
        with patch.object(conftest, "make_run_dir", return_value=None) as make:
            conftest.pytest_configure(config)
        make.assert_called_once_with(explicit_basetemp=None, keep=0)

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
