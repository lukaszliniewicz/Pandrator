"""Token identity, fail-closed ACL changes, and real Windows regressions (#116)."""

from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pandrator_manager.auth import credentials, windows_acl

SID = "S-1-5-21-111-222-333-1001"
MISLEADING_ENV = {
    "LOGNAME": "SYSTEM",
    "USER": "Administrator",
    "LNAME": "Guest",
    "USERNAME": "NotTheProcessUser",
    "USERDOMAIN": "NotTheProcessDomain",
}


class NativeApiFailureTests(unittest.TestCase):
    def setUp(self):
        self.kernel = mock.Mock()
        self.api = mock.Mock()
        self.api.SetNamedSecurityInfoW.return_value = 0
        self.error = 122
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(windows_acl, "_windows_api", return_value=(self.kernel, self.api)).start()
        mock.patch.object(
            ctypes, "get_last_error", side_effect=lambda: self.error, create=True
        ).start()
        mock.patch.object(
            ctypes,
            "WinError",
            side_effect=lambda code: OSError(code, "native test failure"),
            create=True,
        ).start()

        def open_token(_process, _access, output):
            output._obj.value = 123
            return True

        def token_info(_token, _kind, buffer, _length, needed):
            needed._obj.value = ctypes.sizeof(windows_acl._TokenUser)
            if buffer is None:
                return False
            ctypes.cast(buffer, ctypes.POINTER(windows_acl._TokenUser)).contents.User.Sid = 456
            return True

        def sid_string(_sid, output):
            output._obj.value = SID
            return True

        def descriptor(_sddl, _version, output, _size):
            output._obj.value = 789
            return True

        def dacl(_descriptor, present, output, _defaulted):
            present._obj.value = 1
            output._obj.value = 987
            return True

        self.api.OpenProcessToken.side_effect = open_token
        self.api.GetTokenInformation.side_effect = token_info
        self.api.ConvertSidToStringSidW.side_effect = sid_string
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = descriptor
        self.api.GetSecurityDescriptorDacl.side_effect = dacl

    def test_identity_comes_from_process_token_despite_environment(self):
        with mock.patch.dict(os.environ, MISLEADING_ENV):
            self.assertEqual(SID, windows_acl.process_user_sid())
        self.assertEqual(0x0008, self.api.OpenProcessToken.call_args.args[1])
        self.assertEqual(
            [1, 1], [call.args[1] for call in self.api.GetTokenInformation.call_args_list]
        )
        self.kernel.CloseHandle.assert_called_once()
        self.kernel.LocalFree.assert_called_once()

    def test_token_open_failure_never_changes_acl(self):
        self.api.OpenProcessToken.side_effect = None
        self.api.OpenProcessToken.return_value = False
        self.error = 5
        with self.assertRaises(OSError):
            windows_acl.protect_windows_path(Path("unused"))
        self.api.SetNamedSecurityInfoW.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_token_size_failure_closes_handle_without_changing_acl(self):
        self.error = 5
        with self.assertRaises(OSError):
            windows_acl.protect_windows_path(Path("unused"))
        self.kernel.CloseHandle.assert_called_once()
        self.api.ConvertSidToStringSidW.assert_not_called()
        self.api.SetNamedSecurityInfoW.assert_not_called()

    def test_token_read_failure_closes_handle(self):
        original = self.api.GetTokenInformation.side_effect

        def fail_read(token, kind, buffer, length, needed):
            if buffer is not None:
                self.error = 5
                return False
            return original(token, kind, buffer, length, needed)

        self.api.GetTokenInformation.side_effect = fail_read
        with self.assertRaises(OSError):
            windows_acl.process_user_sid()
        self.kernel.CloseHandle.assert_called_once()
        self.api.ConvertSidToStringSidW.assert_not_called()

    def test_sid_conversion_failure_closes_handle_without_fallback(self):
        self.api.ConvertSidToStringSidW.side_effect = None
        self.api.ConvertSidToStringSidW.return_value = False
        with mock.patch.dict(os.environ, MISLEADING_ENV), self.assertRaises(OSError):
            windows_acl.protect_windows_path(Path("unused"))
        self.kernel.CloseHandle.assert_called_once()
        self.kernel.LocalFree.assert_not_called()
        self.api.SetNamedSecurityInfoW.assert_not_called()

    def test_complete_dacl_has_only_token_user_and_correct_inheritance(self):
        for directory, inheritance in ((False, ""), (True, "OICI")):
            with self.subTest(directory=directory):
                windows_acl.protect_windows_path(Path("unused"), directory=directory)
                self.assertEqual(
                    f"D:P(A;{inheritance};FA;;;{SID})",
                    self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.call_args.args[0],
                )
                args = self.api.SetNamedSecurityInfoW.call_args.args
                self.assertEqual(("unused", 1, 0x80000004, None, None), args[:5])
                self.assertIsNone(args[6])  # Do not modify ownership or auditing.
                self.assertEqual(987, args[5].value)

    def test_null_dacl_is_never_applied_and_descriptor_is_freed(self):
        self.api.GetSecurityDescriptorDacl.side_effect = lambda *_args: True
        with self.assertRaisesRegex(OSError, "restrictive"):
            windows_acl.protect_windows_path(Path("unused"))
        self.api.SetNamedSecurityInfoW.assert_not_called()
        self.assertEqual(2, self.kernel.LocalFree.call_count)

    def test_descriptor_conversion_failure_never_changes_path(self):
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = None
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.return_value = False
        with self.assertRaises(OSError):
            windows_acl.protect_windows_path(Path("unused"))
        self.api.SetNamedSecurityInfoW.assert_not_called()
        self.assertEqual(1, self.kernel.LocalFree.call_count)  # Only the SID string.

    def test_dacl_read_failure_frees_descriptor_without_applying(self):
        self.api.GetSecurityDescriptorDacl.side_effect = None
        self.api.GetSecurityDescriptorDacl.return_value = False
        with self.assertRaises(OSError):
            windows_acl.protect_windows_path(Path("unused"))
        self.api.SetNamedSecurityInfoW.assert_not_called()
        self.assertEqual(2, self.kernel.LocalFree.call_count)

    def test_apply_failure_is_reported_and_native_allocations_are_freed(self):
        self.api.SetNamedSecurityInfoW.return_value = 5
        with self.assertRaises(OSError):
            windows_acl.protect_windows_path(Path("unused"))
        self.assertEqual(2, self.kernel.LocalFree.call_count)
        self.kernel.CloseHandle.assert_called_once()


class ProtectionDispatchTests(unittest.TestCase):
    def test_windows_failure_has_actionable_context(self):
        with (
            mock.patch.object(credentials, "os", SimpleNamespace(name="nt")),
            mock.patch.object(
                windows_acl, "protect_windows_path", side_effect=OSError(5, "denied")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "targeted recovery"):
                credentials.protect_path(Path("manager-state"))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_posix_file_and_directory_modes_are_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "secret"
            path.write_text("test credential", encoding="utf-8")
            credentials.protect_path(root, directory=True)
            credentials.protect_path(path)
            self.assertEqual(0o700, root.stat().st_mode & 0o777)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)


def _powershell(script: str) -> dict:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _acl_info(path: Path) -> dict:
    literal = str(path).replace("'", "''")
    return _powershell(
        "$ErrorActionPreference = 'Stop'; "
        f"$acl = Get-Acl -LiteralPath '{literal}'; "
        "$sid = [System.Security.Principal.SecurityIdentifier]; "
        "@{process_sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "owner=$acl.GetOwner($sid).Value; protected=$acl.AreAccessRulesProtected; "
        "rules=@($acl.GetAccessRules($true,$true,$sid) | ForEach-Object { "
        "@{sid=$_.IdentityReference.Value; rights=[int]$_.FileSystemRights; "
        "type=$_.AccessControlType.ToString(); inherited=$_.IsInherited; "
        "inheritance=[int]$_.InheritanceFlags} })} | ConvertTo-Json -Depth 4"
    )


@unittest.skipUnless(os.name == "nt", "Real Windows access-token and NTFS tests")
class WindowsAclIntegrationTests(unittest.TestCase):
    def test_token_matches_windows_identity_when_all_names_are_misleading(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, MISLEADING_ENV),
        ):
            info = _acl_info(Path(directory))
            self.assertEqual(info["process_sid"], windows_acl.process_user_sid())

    def test_files_directories_and_inherited_children_stay_accessible(self):
        with tempfile.TemporaryDirectory(prefix="pandrator-acl-") as directory:
            root = Path(directory) / "Unicode-żółć with spaces"
            root.mkdir()
            original_owner = _acl_info(root)["owner"]
            with mock.patch.dict(os.environ, MISLEADING_ENV):
                credentials.protect_path(root, directory=True)
                info = _acl_info(root)
                self.assertEqual(original_owner, info["owner"])
                self.assertTrue(info["protected"])
                self.assertEqual(1, len(info["rules"]))
                rule = info["rules"][0]
                self.assertEqual(info["process_sid"], rule["sid"])
                self.assertEqual(2032127, rule["rights"])  # FILE_ALL_ACCESS
                self.assertEqual("Allow", rule["type"])
                self.assertEqual(3, rule["inheritance"])  # Files and directories.
                child = root / "nested"
                child.mkdir()
                path = child / "secret.txt"
                path.write_text("round trip", encoding="utf-8")
                inherited = _acl_info(path)
                self.assertEqual([info["process_sid"]], [r["sid"] for r in inherited["rules"]])
                credentials.protect_path(path)
                credentials.protect_path(path)  # Idempotent.
                self.assertEqual("round trip", path.read_text(encoding="utf-8"))
                path.write_text("replaced", encoding="utf-8")
                path.rename(child / "renamed.txt")
                protected = _acl_info(child / "renamed.txt")
                self.assertTrue(protected["protected"])
                self.assertEqual(1, len(protected["rules"]))
                self.assertEqual(info["process_sid"], protected["rules"][0]["sid"])
                self.assertEqual(0, protected["rules"][0]["inheritance"])

    def test_existing_explicit_grants_do_not_survive_protection(self):
        with tempfile.TemporaryDirectory(prefix="pandrator-acl-") as directory:
            path = Path(directory) / "state.txt"
            path.write_text("private", encoding="utf-8")
            subprocess.run(
                ["icacls", str(path), "/grant", "*S-1-1-0:R"],
                check=True,
                capture_output=True,
                timeout=30,
            )
            self.assertIn("S-1-1-0", [r["sid"] for r in _acl_info(path)["rules"]])
            with mock.patch.dict(os.environ, MISLEADING_ENV):
                credentials.protect_path(path)
            info = _acl_info(path)
            self.assertEqual([info["process_sid"]], [r["sid"] for r in info["rules"]])
            self.assertEqual("private", path.read_text(encoding="utf-8"))

    def test_launcher_copy_hash_and_atomic_credentials_with_poisoned_names(self):
        # Exercise the exact copy/protect/reopen/replace sequence from #116,
        # plus the shared atomic-secret helper, without installing a launcher.
        import hashlib
        import shutil
        import sys

        with (
            tempfile.TemporaryDirectory(prefix="pandrator-acl-") as directory,
            mock.patch.dict(os.environ, MISLEADING_ENV),
        ):
            root = Path(directory)
            credentials.protect_path(root, directory=True)
            staging = root / ".launcher.tmp"
            shutil.copyfile(sys.executable, staging)
            expected = hashlib.sha256(staging.read_bytes()).hexdigest()
            credentials.protect_path(staging)
            self.assertEqual(expected, hashlib.sha256(staging.read_bytes()).hexdigest())
            destination = root / "launcher.exe"
            staging.replace(destination)
            credentials.protect_path(destination)
            self.assertEqual(expected, hashlib.sha256(destination.read_bytes()).hexdigest())
            secret_path = root / "client-secret"
            secret = credentials.ensure_client_secret(secret_path)
            self.assertEqual(secret, credentials.read_client_secret(secret_path))
            self.assertEqual(secret, credentials.ensure_client_secret(secret_path))


if __name__ == "__main__":
    unittest.main()
