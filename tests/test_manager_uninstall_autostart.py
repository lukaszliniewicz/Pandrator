from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock

from pandrator_manager import tray_autostart as tray_autostart_module
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.tray_autostart import configure_tray_autostart


class _FakeRegistryKey:
    def __enter__(self) -> _FakeRegistryKey:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeWinReg(ModuleType):
    HKEY_CURRENT_USER = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__("winreg")
        self.values = values

    def CreateKeyEx(
        self,
        _hive: int,
        _sub_key: str,
        _reserved: int,
        _access: int,
    ) -> _FakeRegistryKey:
        return _FakeRegistryKey()

    def DeleteValue(self, _key: _FakeRegistryKey, name: str) -> None:
        try:
            del self.values[name]
        except KeyError as error:
            raise FileNotFoundError(name) from error

    def SetValueEx(
        self,
        _key: _FakeRegistryKey,
        name: str,
        _reserved: int,
        _value_type: int,
        value: str,
    ) -> None:
        self.values[name] = value


class ManagerUninstallAutostartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True,
        )
        self.addCleanup(self.temporary.cleanup)
        self.environment_patch = mock.patch.dict(
            os.environ,
            {
                "APPDATA": str(Path(self.temporary.name) / "appdata"),
                "XDG_CONFIG_HOME": str(Path(self.temporary.name) / "xdg-config"),
            },
        )
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        self.workspace = Path(self.temporary.name) / "workspace"
        self.workspace.mkdir()
        self.layout = WorkspaceLayout.from_value(self.workspace)

    def _tray_suffix(self) -> str:
        return hashlib.sha256(
            str(self.layout.workspace).encode("utf-8"),
        ).hexdigest()[:10]

    def test_linux_removes_only_workspace_file_and_is_idempotent(self) -> None:
        config_home = Path(os.environ["XDG_CONFIG_HOME"])
        autostart = config_home / "autostart"
        expected = autostart / f"pandrator-tray-{self._tray_suffix()}.desktop"
        unrelated = autostart / "unrelated.desktop"

        with (
            mock.patch.object(tray_autostart_module.sys, "platform", "linux"),
            mock.patch.object(
                tray_autostart_module,
                "_tray_command",
                return_value=("python", "-m", "pandrator_manager.tray"),
            ),
        ):
            created = configure_tray_autostart(self.layout, enabled=True)
            self.assertEqual(created, expected)
            unrelated.write_text("leave me", encoding="utf-8")

            removed = configure_tray_autostart(self.layout, enabled=False)
            repeated = configure_tray_autostart(self.layout, enabled=False)

        self.assertEqual(removed, expected)
        self.assertEqual(repeated, expected)
        self.assertFalse(expected.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "leave me")

    def test_windows_removes_only_workspace_value_and_legacy_file(self) -> None:
        suffix = self._tray_suffix()
        value_name = f"PandratorTray-{suffix}"
        foreign_value = "AnotherProduct"
        registry = _FakeWinReg(
            {
                value_name: '"C:\\Pandrator\\manager.exe" tray',
                foreign_value: '"C:\\Other\\manager.exe"',
            },
        )
        startup = (
            Path(os.environ["APPDATA"])
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup"
        )
        matching_file = startup / f"PandratorTray-{suffix}.cmd"
        unrelated_file = startup / "unrelated.cmd"
        startup.mkdir(parents=True)
        matching_file.write_text("owned", encoding="utf-8")
        unrelated_file.write_text("leave me", encoding="utf-8")

        with (
            mock.patch.object(tray_autostart_module.sys, "platform", "win32"),
            mock.patch.dict(sys.modules, {"winreg": registry}),
        ):
            result = configure_tray_autostart(self.layout, enabled=False)
            repeated = configure_tray_autostart(self.layout, enabled=False)

        expected_location = f"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run\\{value_name}"
        self.assertEqual(result, expected_location)
        self.assertEqual(repeated, expected_location)
        self.assertEqual(registry.values, {foreign_value: '"C:\\Other\\manager.exe"'})
        self.assertFalse(matching_file.exists())
        self.assertEqual(unrelated_file.read_text(encoding="utf-8"), "leave me")


if __name__ == "__main__":
    unittest.main()
