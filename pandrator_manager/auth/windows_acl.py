"""Windows-only, token-derived protection for Manager-owned state paths.

No environment variables, account-name resolution, shell, or optional packages
participate in the security decision. Keep the native bindings lazy so importing
Manager remains safe on other platforms and inside the frozen bootstrap.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_SE_FILE_OBJECT = 1
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SDDL_REVISION_1 = 1


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("User", _SidAndAttributes)]


@lru_cache(maxsize=1)
def _windows_api():
    if os.name != "nt":
        raise OSError("Windows path protection requires Windows.")
    # Pointer-sized handles and explicit prototypes are essential on Win64.
    # System32-only lookup avoids both PATH and working-directory DLL searches.
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True, winmode=0x00000800)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True, winmode=0x00000800)
    pointer = ctypes.c_void_p
    prototypes = (
        (kernel32.GetCurrentProcess, [], wintypes.HANDLE),
        (kernel32.CloseHandle, [wintypes.HANDLE], wintypes.BOOL),
        (kernel32.LocalFree, [pointer], pointer),
        (
            advapi32.OpenProcessToken,
            [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)],
            wintypes.BOOL,
        ),
        (
            advapi32.GetTokenInformation,
            [
                wintypes.HANDLE,
                ctypes.c_int,
                pointer,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ],
            wintypes.BOOL,
        ),
        (
            advapi32.ConvertSidToStringSidW,
            [pointer, ctypes.POINTER(wintypes.LPWSTR)],
            wintypes.BOOL,
        ),
        (
            advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(pointer),
                ctypes.POINTER(wintypes.ULONG),
            ],
            wintypes.BOOL,
        ),
        (
            advapi32.GetSecurityDescriptorDacl,
            [
                pointer,
                ctypes.POINTER(wintypes.BOOL),
                ctypes.POINTER(pointer),
                ctypes.POINTER(wintypes.BOOL),
            ],
            wintypes.BOOL,
        ),
        (
            advapi32.SetNamedSecurityInfoW,
            [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, pointer, pointer, pointer, pointer],
            wintypes.DWORD,
        ),
    )
    for function, argtypes, restype in prototypes:
        function.argtypes = argtypes
        function.restype = restype
    return kernel32, advapi32


def process_user_sid() -> str:
    """Read TokenUser from this process, not the desktop user or environment.

    Manager is a per-user process and does not impersonate clients. The process
    token is consequently the authority even under RunAs, SSH, or elevation.
    Do not cache identity or fall back to USERNAME if any native call fails.
    """
    kernel32, advapi32 = _windows_api()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        needed = wintypes.DWORD()
        ok = advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(needed))
        error = ctypes.get_last_error()
        if ok or error != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(error, "Could not size the Windows process TokenUser.")
        if needed.value < ctypes.sizeof(_TokenUser):
            raise OSError("Windows returned an invalid TokenUser buffer size.")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, _TOKEN_USER, buffer, needed.value, ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(user.User.Sid, ctypes.byref(sid_text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            sid = sid_text.value
            if not sid or not sid.startswith("S-1-"):
                raise OSError("Windows returned an invalid process user SID.")
            return sid
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def protect_windows_path(path: Path, *, directory: bool = False) -> None:
    """Replace a Manager-owned path's DACL with process-user-only access.

    Build and validate the complete ACL before the single native update. Merely
    granting the correct SID would leave an old explicit wrong-account grant
    behind. Ownership and audit rules are not changed; no privileges are enabled
    and no recursive ownership recovery is attempted.
    """
    sid = process_user_sid()  # Identity failure must precede any path mutation.
    kernel32, advapi32 = _windows_api()
    inheritance = "OICI" if directory else ""
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;{inheritance};FA;;;{sid})",
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # A NULL DACL grants everyone access; never pass one to the setter.
        if not present.value or not dacl.value:
            raise OSError("Windows did not produce a restrictive Manager DACL.")
        error = advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(descriptor)
