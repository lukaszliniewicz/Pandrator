"""Stream embedded-resource updates into a GGUF v3 file."""

from __future__ import annotations

import ctypes
import errno
import os
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

_HEADER = struct.Struct("<4sIQQ")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_CHUNK_SIZE = 1024 * 1024
_MAX_ALIGNMENT = 1024 * 1024
_MAX_KEY_BYTES = 1024 * 1024
_MAX_RESOURCE_NAME_BYTES = 4096
_MAX_RESOURCE_COUNT = 1_000_000

_GGUF_VALUE_SIZES = {
    0: 1,  # UINT8
    1: 1,  # INT8
    2: 2,  # UINT16
    3: 2,  # INT16
    4: 4,  # UINT32
    5: 4,  # INT32
    6: 4,  # FLOAT32
    7: 1,  # BOOL
    10: 8,  # UINT64
    11: 8,  # INT64
    12: 8,  # FLOAT64
}
_RESOURCE_KEYS = (
    "audiocpp.embedded_files.names",
    "audiocpp.embedded_files.offsets",
    "audiocpp.embedded_files.data",
)
_RESOURCE_TYPES = {
    _RESOURCE_KEYS[0]: 8,  # STRING
    _RESOURCE_KEYS[1]: 10,  # UINT64
    _RESOURCE_KEYS[2]: 0,  # UINT8
}


class _Reader:
    def __init__(self, handle, file_size: int) -> None:
        self.handle = handle
        self.file_size = file_size

    def tell(self) -> int:
        return self.handle.tell()

    def read(self, length: int) -> bytes:
        if length < 0 or length > self.file_size - self.tell():
            raise ValueError("GGUF field extends beyond the input file.")
        value = self.handle.read(length)
        if len(value) != length:
            raise ValueError("GGUF input ended inside a field.")
        return value

    def skip(self, length: int) -> None:
        if length < 0 or length > self.file_size - self.tell():
            raise ValueError("GGUF field extends beyond the input file.")
        self.handle.seek(length, os.SEEK_CUR)

    def u32(self) -> int:
        return _U32.unpack(self.read(_U32.size))[0]

    def u64(self) -> int:
        return _U64.unpack(self.read(_U64.size))[0]

    def string_bytes(self, *, maximum: int | None = None) -> bytes:
        length = self.u64()
        if maximum is not None and length > maximum:
            raise ValueError("GGUF string exceeds the supported size.")
        return self.read(length)

    def skip_string(self) -> None:
        length = self.u64()
        self.skip(length)


@dataclass(frozen=True, slots=True)
class _MetadataEntry:
    key: str
    start: int
    end: int
    value_prefix_end: int


@dataclass(frozen=True, slots=True)
class _InputLayout:
    file_size: int
    tensor_count: int
    metadata_count: int
    tensor_info_start: int
    tensor_info_end: int
    tensor_data_start: int
    alignment: int
    entries: tuple[_MetadataEntry, ...]
    resource_names: tuple[str, ...]
    resource_offsets: tuple[int, ...]
    resource_data_start: int
    resource_data_size: int


def _decode_utf8(value: bytes, description: str) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"GGUF {description} is not valid UTF-8.") from exc


def _validate_resource_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("Embedded resource names must be non-empty strings.")
    encoded = name.encode("utf-8")
    if len(encoded) > _MAX_RESOURCE_NAME_BYTES:
        raise ValueError("Embedded resource name exceeds the supported size.")
    if "\\" in name or ":" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"Unsafe embedded resource path: {name!r}")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != name
    ):
        raise ValueError(f"Unsafe embedded resource path: {name!r}")


def _skip_value(reader: _Reader, value_type: int) -> None:
    if value_type == 8:  # STRING
        reader.skip_string()
        return
    if value_type == 9:  # ARRAY
        subtype = reader.u32()
        count = reader.u64()
        if subtype == 9 or subtype not in _GGUF_VALUE_SIZES and subtype != 8:
            raise ValueError("GGUF array has an unsupported element type.")
        if subtype == 8:
            if count > (reader.file_size - reader.tell()) // _U64.size:
                raise ValueError("GGUF string array count exceeds the input file.")
            for _ in range(count):
                reader.skip_string()
            return
        element_size = _GGUF_VALUE_SIZES[subtype]
        if count > (reader.file_size - reader.tell()) // element_size:
            raise ValueError("GGUF array count exceeds the input file.")
        if subtype == 7:
            for _ in range(count):
                if reader.read(1)[0] not in (0, 1):
                    raise ValueError("GGUF boolean array contains an invalid value.")
            return
        reader.skip(count * element_size)
        return
    if value_type not in _GGUF_VALUE_SIZES:
        raise ValueError(f"GGUF metadata has unsupported value type {value_type}.")
    value = reader.read(_GGUF_VALUE_SIZES[value_type])
    if value_type == 7 and value[0] not in (0, 1):
        raise ValueError("GGUF boolean value is invalid.")


def _parse_input(handle, file_size: int, check_cancelled: Callable[[], None]) -> _InputLayout:
    reader = _Reader(handle, file_size)
    if file_size < _HEADER.size:
        raise ValueError("Input is too short to be a GGUF file.")
    magic, version, tensor_count, metadata_count = _HEADER.unpack(reader.read(_HEADER.size))
    if magic != b"GGUF":
        raise ValueError("Input does not have a GGUF header.")
    if version != 3:
        raise ValueError("Only GGUF version 3 is supported.")
    if metadata_count > (file_size - _HEADER.size) // 13:
        raise ValueError("GGUF metadata count exceeds the input file.")
    if tensor_count > (file_size - _HEADER.size) // 24:
        raise ValueError("GGUF tensor count exceeds the input file.")

    entries: list[_MetadataEntry] = []
    seen_keys: set[str] = set()
    alignment = 32
    resource_names: tuple[str, ...] = ()
    resource_offsets: tuple[int, ...] = ()
    resource_data_start = 0
    resource_data_size = 0
    resource_presence: set[str] = set()

    for _ in range(metadata_count):
        check_cancelled()
        entry_start = reader.tell()
        key_bytes = reader.string_bytes(maximum=_MAX_KEY_BYTES)
        key = _decode_utf8(key_bytes, "metadata key")
        if not key:
            raise ValueError("GGUF metadata keys must be non-empty.")
        if key in seen_keys:
            raise ValueError(f"GGUF metadata contains duplicate key {key!r}.")
        seen_keys.add(key)
        value_type = reader.u32()
        value_prefix_end = reader.tell()

        if key == "general.alignment":
            if value_type != 4:
                raise ValueError("GGUF general.alignment must be a UINT32 value.")
            alignment = reader.u32()
        elif key in _RESOURCE_KEYS:
            if value_type != 9:
                raise ValueError(f"GGUF resource entry {key!r} must be an array.")
            subtype = reader.u32()
            count = reader.u64()
            expected_subtype = _RESOURCE_TYPES[key]
            if subtype != expected_subtype:
                raise ValueError(f"GGUF resource entry {key!r} has the wrong array type.")
            if count > _MAX_RESOURCE_COUNT and key != _RESOURCE_KEYS[2]:
                raise ValueError("GGUF embedded resource count exceeds the supported limit.")
            resource_presence.add(key)
            if key == _RESOURCE_KEYS[0]:
                if count > (file_size - reader.tell()) // _U64.size:
                    raise ValueError("GGUF resource name count exceeds the input file.")
                names: list[str] = []
                for _ in range(count):
                    raw_name = reader.string_bytes(maximum=_MAX_RESOURCE_NAME_BYTES)
                    name = _decode_utf8(raw_name, "embedded resource name")
                    _validate_resource_name(name)
                    names.append(name)
                resource_names = tuple(names)
            elif key == _RESOURCE_KEYS[1]:
                if count > (file_size - reader.tell()) // _U64.size:
                    raise ValueError("GGUF resource offset count exceeds the input file.")
                resource_offsets = tuple(reader.u64() for _ in range(count))
            else:
                resource_data_start = reader.tell()
                resource_data_size = count
                reader.skip(count)
        else:
            _skip_value(reader, value_type)

        entries.append(_MetadataEntry(key, entry_start, reader.tell(), value_prefix_end))

    metadata_end = reader.tell()
    if alignment < 1 or alignment > _MAX_ALIGNMENT or alignment & (alignment - 1):
        raise ValueError("GGUF alignment must be a supported power of two.")
    if resource_presence and resource_presence != set(_RESOURCE_KEYS):
        raise ValueError("GGUF embedded resource metadata is incomplete.")
    if resource_presence:
        if len(set(resource_names)) != len(resource_names):
            raise ValueError("GGUF embedded resource names contain duplicates.")
        if len(resource_offsets) != len(resource_names) + 1:
            raise ValueError("GGUF embedded resource offsets do not match the names.")
        if not resource_offsets or resource_offsets[0] != 0:
            raise ValueError("GGUF embedded resource offsets must start at zero.")
        if any(
            left > right
            for left, right in zip(
                resource_offsets[:-1], resource_offsets[1:], strict=True
            )
        ):
            raise ValueError("GGUF embedded resource offsets must be monotone.")
        if resource_offsets[-1] != resource_data_size:
            raise ValueError("GGUF embedded resource offsets do not end at the data length.")
    else:
        resource_names = ()
        resource_offsets = (0,)
        resource_data_start = 0
        resource_data_size = 0

    tensor_info_start = reader.tell()
    seen_tensors: set[str] = set()
    for _ in range(tensor_count):
        check_cancelled()
        name_bytes = reader.string_bytes(maximum=_MAX_KEY_BYTES)
        name = _decode_utf8(name_bytes, "tensor name")
        if not name or name in seen_tensors:
            raise ValueError("GGUF tensor names must be non-empty and unique.")
        seen_tensors.add(name)
        dimensions = reader.u32()
        if dimensions < 1 or dimensions > 4:
            raise ValueError("GGUF tensor dimension count is out of range.")
        for _ in range(dimensions):
            if reader.u64() == 0:
                raise ValueError("GGUF tensor dimensions must be positive.")
        reader.u32()  # Tensor type remains opaque; payloads are never decoded.
        tensor_offset = reader.u64()
        if tensor_offset % alignment:
            raise ValueError("GGUF tensor offset is not aligned.")

    tensor_info_end = reader.tell()
    tensor_data_start = (tensor_info_end + alignment - 1) & ~(alignment - 1)
    if tensor_data_start > file_size:
        raise ValueError("GGUF tensor data alignment extends beyond the input file.")
    tensor_data_size = file_size - tensor_data_start
    handle.seek(tensor_info_start)
    for _ in range(tensor_count):
        reader.string_bytes(maximum=_MAX_KEY_BYTES)
        dimensions = reader.u32()
        reader.skip(dimensions * _U64.size)
        reader.u32()
        tensor_offset = reader.u64()
        if tensor_offset >= tensor_data_size:
            raise ValueError("GGUF tensor offset is outside the tensor data region.")
    handle.seek(metadata_end)

    return _InputLayout(
        file_size=file_size,
        tensor_count=tensor_count,
        metadata_count=metadata_count,
        tensor_info_start=tensor_info_start,
        tensor_info_end=tensor_info_end,
        tensor_data_start=tensor_data_start,
        alignment=alignment,
        entries=tuple(entries),
        resource_names=resource_names,
        resource_offsets=resource_offsets,
        resource_data_start=resource_data_start,
        resource_data_size=resource_data_size,
    )


def _copy_range(
    source,
    destination,
    start: int,
    length: int,
    check_cancelled: Callable[[], None],
) -> None:
    source.seek(start)
    remaining = length
    while remaining:
        check_cancelled()
        chunk = source.read(min(_CHUNK_SIZE, remaining))
        if not chunk:
            raise ValueError("GGUF input changed or ended during copying.")
        destination.write(chunk)
        remaining -= len(chunk)


def _write_string(handle, value: str) -> None:
    encoded = value.encode("utf-8")
    handle.write(_U64.pack(len(encoded)))
    handle.write(encoded)


def _write_array_header(handle, subtype: int, count: int) -> None:
    handle.write(_U32.pack(subtype))
    handle.write(_U64.pack(count))


def _write_resource_names(handle, names: tuple[str, ...]) -> None:
    _write_array_header(handle, _RESOURCE_TYPES[_RESOURCE_KEYS[0]], len(names))
    for name in names:
        _write_string(handle, name)


def _write_resource_offsets(handle, offsets: tuple[int, ...]) -> None:
    _write_array_header(handle, _RESOURCE_TYPES[_RESOURCE_KEYS[1]], len(offsets))
    for offset in offsets:
        handle.write(_U64.pack(offset))


def _write_new_entry_prefix(handle, key: str) -> None:
    encoded = key.encode("utf-8")
    handle.write(_U64.pack(len(encoded)))
    handle.write(encoded)
    handle.write(_U32.pack(9))  # ARRAY


def _rename_without_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        # Windows os.rename fails when the destination already exists.
        os.rename(source, destination)
        return

    if os.name == "posix":
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
        except (AttributeError, OSError):
            renameat2 = None
        if renameat2 is not None:
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(destination),
                1,  # RENAME_NOREPLACE
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError(error_number, os.strerror(error_number), str(destination))

    # Same-directory hard-link creation is atomic and cannot replace a path.
    os.link(source, destination)
    try:
        source.unlink()
    except OSError:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def embed_gguf_files(
    source: Path,
    destination: Path,
    files: Mapping[str, Path],
    check_cancelled: Callable[[], None],
) -> None:
    """Copy a GGUF v3 file while adding resources to its embedded-file arrays."""
    check_cancelled()
    source_path = Path(source).expanduser().resolve(strict=True)
    if not source_path.is_file():
        raise ValueError("GGUF source must be a regular file.")

    destination_path = Path(os.path.abspath(Path(destination).expanduser()))
    if destination_path == source_path:
        raise ValueError("GGUF source and destination must be different files.")
    if os.path.lexists(destination_path):
        if os.path.isfile(destination_path) and os.path.samefile(source_path, destination_path):
            raise ValueError("GGUF source and destination must not be the same file or hard link.")
        raise FileExistsError(destination_path)
    if not destination_path.parent.is_dir():
        raise FileNotFoundError(f"GGUF destination directory does not exist: {destination_path.parent}")
    try:
        if destination_path.resolve(strict=False) == source_path:
            raise ValueError("GGUF source and destination must be different files.")
    except OSError:
        pass

    supplied_names = tuple(sorted(files))
    for name in supplied_names:
        _validate_resource_name(name)
    if len(set(supplied_names)) != len(supplied_names):
        raise ValueError("Supplied embedded resource names contain duplicates.")

    resource_paths: dict[str, Path] = {}
    resource_sizes: dict[str, int] = {}
    for name in supplied_names:
        resource_path = Path(files[name]).expanduser()
        try:
            resource_size = resource_path.stat().st_size
        except OSError as exc:
            raise ValueError(f"Could not inspect embedded resource {name!r}.") from exc
        if not resource_path.is_file() or resource_size < 0:
            raise ValueError(f"Embedded resource {name!r} must be a regular file.")
        resource_paths[name] = resource_path
        resource_sizes[name] = resource_size

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".tmp",
        dir=destination_path.parent,
    )
    temporary_path = Path(temporary_name)
    published = False
    try:
        with source_path.open("rb") as source_handle:
            source_stat = os.fstat(source_handle.fileno())
            layout = _parse_input(source_handle, source_stat.st_size, check_cancelled)
            if set(layout.resource_names).intersection(supplied_names):
                raise ValueError("Supplied embedded resource name already exists in the GGUF file.")
            if len(layout.resource_names) + len(supplied_names) > _MAX_RESOURCE_COUNT:
                raise ValueError("GGUF embedded resource count exceeds the supported limit.")

            names = layout.resource_names + supplied_names
            offsets = list(layout.resource_offsets)
            total_resource_bytes = layout.resource_data_size
            for name in supplied_names:
                total_resource_bytes += resource_sizes[name]
                if total_resource_bytes > 0xFFFFFFFFFFFFFFFF:
                    raise ValueError("Combined embedded resource data exceeds the GGUF limit.")
                offsets.append(total_resource_bytes)
            final_offsets = tuple(offsets)

            has_resource_arrays = any(
                entry.key in _RESOURCE_KEYS for entry in layout.entries
            )
            new_metadata_count = layout.metadata_count + (
                0 if has_resource_arrays else 3
            )
            if new_metadata_count > 0xFFFFFFFFFFFFFFFF:
                raise ValueError("GGUF metadata count exceeds the supported range.")

            with os.fdopen(descriptor, "wb") as output_handle:
                descriptor = -1
                output_handle.write(
                    _HEADER.pack(b"GGUF", 3, layout.tensor_count, new_metadata_count)
                )
                for entry in layout.entries:
                    if entry.key not in _RESOURCE_KEYS:
                        _copy_range(
                            source_handle,
                            output_handle,
                            entry.start,
                            entry.end - entry.start,
                            check_cancelled,
                        )
                        continue

                    _copy_range(
                        source_handle,
                        output_handle,
                        entry.start,
                        entry.value_prefix_end - entry.start,
                        check_cancelled,
                    )
                    if entry.key == _RESOURCE_KEYS[0]:
                        _write_resource_names(output_handle, names)
                    elif entry.key == _RESOURCE_KEYS[1]:
                        _write_resource_offsets(output_handle, final_offsets)
                    else:
                        _write_array_header(
                            output_handle,
                            _RESOURCE_TYPES[_RESOURCE_KEYS[2]],
                            total_resource_bytes,
                        )
                        _copy_range(
                            source_handle,
                            output_handle,
                            layout.resource_data_start,
                            layout.resource_data_size,
                            check_cancelled,
                        )
                        for name in supplied_names:
                            check_cancelled()
                            path = resource_paths[name]
                            expected_size = resource_sizes[name]
                            with path.open("rb") as resource_handle:
                                current = os.fstat(resource_handle.fileno())
                                if current.st_size != expected_size:
                                    raise ValueError(
                                        f"Embedded resource {name!r} changed before copying."
                                    )
                                remaining = expected_size
                                while remaining:
                                    check_cancelled()
                                    chunk = resource_handle.read(min(_CHUNK_SIZE, remaining))
                                    if not chunk:
                                        raise ValueError(
                                            f"Embedded resource {name!r} ended during copying."
                                        )
                                    output_handle.write(chunk)
                                    remaining -= len(chunk)
                                if resource_handle.read(1):
                                    raise ValueError(
                                        f"Embedded resource {name!r} grew during copying."
                                    )

                if not has_resource_arrays:
                    _write_new_entry_prefix(output_handle, _RESOURCE_KEYS[0])
                    _write_resource_names(output_handle, names)
                    _write_new_entry_prefix(output_handle, _RESOURCE_KEYS[1])
                    _write_resource_offsets(output_handle, final_offsets)
                    _write_new_entry_prefix(output_handle, _RESOURCE_KEYS[2])
                    _write_array_header(
                        output_handle,
                        _RESOURCE_TYPES[_RESOURCE_KEYS[2]],
                        total_resource_bytes,
                    )
                    for name in supplied_names:
                        check_cancelled()
                        path = resource_paths[name]
                        expected_size = resource_sizes[name]
                        with path.open("rb") as resource_handle:
                            if os.fstat(resource_handle.fileno()).st_size != expected_size:
                                raise ValueError(
                                    f"Embedded resource {name!r} changed before copying."
                                )
                            remaining = expected_size
                            while remaining:
                                check_cancelled()
                                chunk = resource_handle.read(min(_CHUNK_SIZE, remaining))
                                if not chunk:
                                    raise ValueError(
                                        f"Embedded resource {name!r} ended during copying."
                                    )
                                output_handle.write(chunk)
                                remaining -= len(chunk)
                            if resource_handle.read(1):
                                raise ValueError(
                                    f"Embedded resource {name!r} grew during copying."
                                )

                _copy_range(
                    source_handle,
                    output_handle,
                    layout.tensor_info_start,
                    layout.tensor_info_end - layout.tensor_info_start,
                    check_cancelled,
                )
                output_padding = (-output_handle.tell()) % layout.alignment
                if output_padding:
                    output_handle.write(b"\x00" * output_padding)
                _copy_range(
                    source_handle,
                    output_handle,
                    layout.tensor_data_start,
                    layout.file_size - layout.tensor_data_start,
                    check_cancelled,
                )
                output_handle.flush()
                os.fsync(output_handle.fileno())

        check_cancelled()
        _rename_without_replace(temporary_path, destination_path)
        published = True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if not published:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
