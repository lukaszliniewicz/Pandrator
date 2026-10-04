"""Native file-lock primitives for installer metadata."""

import ctypes
import os


def try_file_lock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock_file(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class _WindowsOffset(ctypes.Structure):
    _fields_ = [("Offset", ctypes.c_uint32), ("OffsetHigh", ctypes.c_uint32)]


class _WindowsOffsetUnion(ctypes.Union):
    _fields_ = [("offset", _WindowsOffset), ("Pointer", ctypes.c_void_p)]


class _WindowsOverlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("offset", _WindowsOffsetUnion),
        ("hEvent", ctypes.c_void_p),
    ]


def _windows_lifecycle_file_operation(fd: int, *, shared: bool | None) -> None:
    import msvcrt

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_file = kernel32.LockFileEx
    lock_file.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsOverlapped),
    ]
    lock_file.restype = ctypes.c_int
    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(_WindowsOverlapped),
    ]
    unlock_file_ex.restype = ctypes.c_int
    handle = msvcrt.get_osfhandle(fd)
    overlapped = _WindowsOverlapped()
    if shared is None:
        result = unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped))
    else:
        result = lock_file(handle, 1 if shared else 3, 0, 1, 0, ctypes.byref(overlapped))
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())


def try_lifecycle_file_lock(fd: int, *, shared: bool) -> None:
    if os.name == "nt":
        _windows_lifecycle_file_operation(fd, shared=shared)
    else:
        import fcntl

        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(fd, mode | fcntl.LOCK_NB)


def unlock_lifecycle_file(fd: int) -> None:
    if os.name == "nt":
        _windows_lifecycle_file_operation(fd, shared=None)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
