from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from pandrator_manager.artifacts.gguf_resources import embed_gguf_files

_HEADER = struct.Struct("<4sIQQ")
_U32 = struct.Struct("<I")
_U64 = struct.Struct("<Q")
_RESOURCE_NAMES = "audiocpp.embedded_files.names"
_RESOURCE_OFFSETS = "audiocpp.embedded_files.offsets"
_RESOURCE_DATA = "audiocpp.embedded_files.data"


def _gguf_string(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return _U64.pack(len(encoded)) + encoded


def _metadata_entry(key: str, value_type: int, value: bytes) -> bytes:
    return _gguf_string(key) + _U32.pack(value_type) + value


def _array(subtype: int, values: list[bytes]) -> bytes:
    return _U32.pack(subtype) + _U64.pack(len(values)) + b"".join(values)


def _resource_metadata(
    names: list[str],
    offsets: list[int],
    data: bytes,
    *,
    subtypes: dict[str, int] | None = None,
) -> list[bytes]:
    subtypes = subtypes or {}
    return [
        _metadata_entry(
            _RESOURCE_NAMES,
            9,
            _array(
                subtypes.get(_RESOURCE_NAMES, 8),
                [_gguf_string(name) for name in names],
            ),
        ),
        _metadata_entry(
            _RESOURCE_OFFSETS,
            9,
            _array(
                subtypes.get(_RESOURCE_OFFSETS, 10),
                [_U64.pack(offset) for offset in offsets],
            ),
        ),
        _metadata_entry(
            _RESOURCE_DATA,
            9,
            _U32.pack(subtypes.get(_RESOURCE_DATA, 0))
            + _U64.pack(len(data))
            + data,
        ),
    ]


def _write_fixture(
    path: Path,
    *,
    version: int = 3,
    alignment: int = 64,
    names: list[str] | None = None,
    offsets: list[int] | None = None,
    resource_data: bytes | None = None,
    resource_subtypes: dict[str, int] | None = None,
    include_resource_metadata: bool = True,
    tensor_payload: bytes = b"\x19" * 18,
) -> bytes:
    names = ["config.json", "vocab.tsv"] if names is None else names
    resource_data = b'{"a":1}\nword\n' if resource_data is None else resource_data
    offsets = [0, 8, len(resource_data)] if offsets is None else offsets

    metadata = [
        _metadata_entry("general.alignment", 4, _U32.pack(alignment)),
        _metadata_entry("fixture.marker", 8, _gguf_string("keep these bytes")),
        _metadata_entry(
            "fixture.values",
            9,
            _array(4, [_U32.pack(17), _U32.pack(23), _U32.pack(41)]),
        ),
    ]
    if include_resource_metadata:
        metadata.extend(
            _resource_metadata(
                names,
                offsets,
                resource_data,
                subtypes=resource_subtypes,
            )
        )

    tensor_info = (
        _gguf_string("weight.q4_0")
        + _U32.pack(1)
        + _U64.pack(32)
        + _U32.pack(2)  # Q4_0, retained as opaque tensor metadata.
        + _U64.pack(0)
    )
    output = bytearray(_HEADER.pack(b"GGUF", version, 1, len(metadata)))
    output.extend(b"".join(metadata))
    output.extend(tensor_info)
    output.extend(b"\x00" * (-len(output) % alignment))
    output.extend(tensor_payload)
    path.write_bytes(output)
    return bytes(output)


def _read_exact(handle, length: int) -> bytes:
    data = handle.read(length)
    assert len(data) == length
    return data


def _read_u32(handle) -> int:
    return _U32.unpack(_read_exact(handle, _U32.size))[0]


def _read_u64(handle) -> int:
    return _U64.unpack(_read_exact(handle, _U64.size))[0]


def _read_string(handle) -> str:
    return _read_exact(handle, _read_u64(handle)).decode("utf-8")


def _decode_fixture(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        magic, version, tensor_count, metadata_count = _HEADER.unpack(
            _read_exact(handle, _HEADER.size)
        )
        metadata: dict[str, object] = {}
        raw_metadata: dict[str, bytes] = {}
        for _ in range(metadata_count):
            start = handle.tell()
            key = _read_string(handle)
            value_type = _read_u32(handle)
            if value_type == 4:
                value: object = _read_u32(handle)
            elif value_type == 8:
                value = _read_string(handle)
            elif value_type == 9:
                subtype = _read_u32(handle)
                count = _read_u64(handle)
                if subtype == 8:
                    value = [_read_string(handle) for _ in range(count)]
                elif subtype == 10:
                    value = [_read_u64(handle) for _ in range(count)]
                elif subtype == 4:
                    value = [_read_u32(handle) for _ in range(count)]
                elif subtype == 0:
                    value = _read_exact(handle, count)
                else:
                    raise AssertionError(f"Unexpected fixture array subtype: {subtype}")
            else:
                raise AssertionError(f"Unexpected fixture metadata type: {value_type}")
            metadata[key] = value
            raw_metadata[key] = path.read_bytes()[start : handle.tell()]

        tensor_info_start = handle.tell()
        tensors = []
        for _ in range(tensor_count):
            tensor_start = handle.tell()
            name = _read_string(handle)
            dimension_count = _read_u32(handle)
            dimensions = [_read_u64(handle) for _ in range(dimension_count)]
            tensor_type = _read_u32(handle)
            offset = _read_u64(handle)
            tensors.append(
                {
                    "name": name,
                    "dimensions": dimensions,
                    "type": tensor_type,
                    "offset": offset,
                    "raw": path.read_bytes()[tensor_start : handle.tell()],
                }
            )
        tensor_info_end = handle.tell()
        alignment = int(metadata.get("general.alignment", 32))
        tensor_data_start = (tensor_info_end + alignment - 1) & ~(alignment - 1)
        handle.seek(tensor_data_start)
        tensor_data = handle.read()

    return {
        "magic": magic,
        "version": version,
        "metadata": metadata,
        "raw_metadata": raw_metadata,
        "tensor_info": path.read_bytes()[tensor_info_start:tensor_info_end],
        "tensors": tensors,
        "tensor_data": tensor_data,
        "tensor_data_start": tensor_data_start,
    }


def _noop() -> None:
    return None


def test_embed_gguf_resources_preserves_metadata_tensor_info_and_opaque_data(tmp_path):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "enriched.gguf"
    resource = tmp_path / "new-resource.bin"
    resource_bytes = bytes(range(251)) * 7
    resource.write_bytes(resource_bytes)
    source_bytes = _write_fixture(source)

    embed_gguf_files(
        source,
        destination,
        {"voices/en-us/voice.bin": resource},
        _noop,
    )

    original = _decode_fixture(source)
    enriched = _decode_fixture(destination)
    assert enriched["magic"] == b"GGUF"
    assert enriched["version"] == 3
    assert enriched["metadata"]["audiocpp.embedded_files.names"] == [
        "config.json",
        "vocab.tsv",
        "voices/en-us/voice.bin",
    ]
    assert enriched["metadata"]["audiocpp.embedded_files.offsets"] == [
        0,
        8,
        len(b'{"a":1}\nword\n'),
        len(b'{"a":1}\nword\n') + len(resource_bytes),
    ]
    data = enriched["metadata"]["audiocpp.embedded_files.data"]
    assert data == b'{"a":1}\nword\n' + resource_bytes
    for key in ("fixture.marker", "fixture.values"):
        assert enriched["raw_metadata"][key] == original["raw_metadata"][key]
    assert enriched["tensor_info"] == original["tensor_info"]
    assert enriched["tensors"] == original["tensors"]
    assert enriched["tensor_data"] == original["tensor_data"]
    assert int(enriched["tensor_data_start"]) % 64 == 0
    assert source.read_bytes() == source_bytes


def test_embed_adds_resource_arrays_when_the_source_has_none(tmp_path):
    source = tmp_path / "plain.gguf"
    destination = tmp_path / "enriched.gguf"
    resource = tmp_path / "table.bin"
    resource.write_bytes(b"table payload")
    _write_fixture(source, include_resource_metadata=False)

    embed_gguf_files(source, destination, {"g2p/table.bin": resource}, _noop)

    decoded = _decode_fixture(destination)
    assert decoded["metadata"]["audiocpp.embedded_files.names"] == ["g2p/table.bin"]
    assert decoded["metadata"]["audiocpp.embedded_files.offsets"] == [0, 13]
    assert decoded["metadata"]["audiocpp.embedded_files.data"] == b"table payload"
    assert decoded["metadata"]["fixture.marker"] == "keep these bytes"


def test_cancel_during_streamed_resource_copy_removes_temporary_output(tmp_path):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "cancelled.gguf"
    resource = tmp_path / "large-resource.bin"
    source_before = _write_fixture(source)
    resource.write_bytes(b"r" * (2 * 1024 * 1024 + 17))
    calls = 0

    def cancel_during_resource_copy() -> None:
        nonlocal calls
        calls += 1
        if calls == 18:
            raise RuntimeError("cancelled")

    with pytest.raises(RuntimeError, match="cancelled"):
        embed_gguf_files(
            source,
            destination,
            {"large/data.bin": resource},
            cancel_during_resource_copy,
        )

    assert not destination.exists()
    assert not list(tmp_path.glob(".cancelled.gguf.*.tmp"))
    assert source.read_bytes() == source_before


def test_existing_destination_is_never_replaced(tmp_path):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "already-there.gguf"
    resource = tmp_path / "resource.bin"
    _write_fixture(source)
    resource.write_bytes(b"resource")
    destination.write_bytes(b"keep this destination")

    with pytest.raises(FileExistsError):
        embed_gguf_files(source, destination, {"resource.bin": resource}, _noop)

    assert destination.read_bytes() == b"keep this destination"
    assert not list(tmp_path.glob(".already-there.gguf.*.tmp"))


def test_symlink_destination_is_never_followed_or_replaced(tmp_path):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "output.gguf"
    target = tmp_path / "target.bin"
    resource = tmp_path / "resource.bin"
    _write_fixture(source)
    target.write_bytes(b"symlink target")
    resource.write_bytes(b"resource")
    destination.symlink_to(target)

    with pytest.raises(FileExistsError):
        embed_gguf_files(source, destination, {"resource.bin": resource}, _noop)

    assert destination.is_symlink()
    assert target.read_bytes() == b"symlink target"
    assert not list(tmp_path.glob(".output.gguf.*.tmp"))


@pytest.mark.parametrize(
    "fixture_options",
    [
        {"version": 2},
        {"resource_subtypes": {_RESOURCE_NAMES: 4}},
        {"offsets": [0, 9, 8]},
        {"offsets": [0, 8, 11]},
        {"names": ["same.bin", "same.bin"]},
        {"names": ["../escape.bin", "vocab.tsv"]},
        {"alignment": 24},
    ],
    ids=["version", "array-type", "nonmonotone-offsets", "wrong-final-offset", "duplicate-name", "unsafe-name", "alignment"],
)
def test_malformed_gguf_is_rejected_without_publishing(tmp_path, fixture_options):
    source = tmp_path / "malformed.gguf"
    destination = tmp_path / "output.gguf"
    _write_fixture(source, **fixture_options)
    resource = tmp_path / "resource.bin"
    resource.write_bytes(b"resource")

    with pytest.raises(ValueError):
        embed_gguf_files(source, destination, {"extra.bin": resource}, _noop)

    assert not destination.exists()
    assert not list(tmp_path.glob(".output.gguf.*.tmp"))


@pytest.mark.parametrize("name", ["../escape.bin", "/absolute.bin", "a\\b.bin", "C:/drive.bin"])
def test_unsafe_supplied_resource_path_is_rejected(tmp_path, name):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "output.gguf"
    resource = tmp_path / "resource.bin"
    _write_fixture(source)
    resource.write_bytes(b"resource")

    with pytest.raises(ValueError, match="Unsafe embedded resource path"):
        embed_gguf_files(source, destination, {name: resource}, _noop)

    assert not destination.exists()


def test_duplicate_supplied_name_is_rejected(tmp_path):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "output.gguf"
    resource = tmp_path / "resource.bin"
    _write_fixture(source)
    resource.write_bytes(b"resource")

    with pytest.raises(ValueError, match="already exists"):
        embed_gguf_files(
            source,
            destination,
            {"config.json": resource},
            _noop,
        )

    assert not destination.exists()


def test_source_alias_and_hardlink_destination_are_rejected(tmp_path):
    source = tmp_path / "source.gguf"
    resource = tmp_path / "resource.bin"
    _write_fixture(source)
    source_bytes = source.read_bytes()
    resource.write_bytes(b"resource")

    with pytest.raises(ValueError, match="different files"):
        embed_gguf_files(source, source, {"new.bin": resource}, _noop)

    hardlink = tmp_path / "hardlink.gguf"
    hardlink.hardlink_to(source)
    with pytest.raises(ValueError, match="same file or hard link"):
        embed_gguf_files(source, hardlink, {"new.bin": resource}, _noop)

    assert source.read_bytes() == source_bytes
    assert hardlink.read_bytes() == source_bytes


def test_cancellation_during_final_sync_does_not_publish(tmp_path, monkeypatch):
    source = tmp_path / "source.gguf"
    destination = tmp_path / "output.gguf"
    resource = tmp_path / "resource.bin"
    _write_fixture(source)
    resource.write_bytes(b"resource")
    cancelled = False
    original_sync = os.fsync

    def sync_and_cancel(descriptor):
        nonlocal cancelled
        original_sync(descriptor)
        cancelled = True

    def check_cancelled():
        if cancelled:
            raise RuntimeError("cancelled before publication")

    monkeypatch.setattr(os, "fsync", sync_and_cancel)
    with pytest.raises(RuntimeError, match="cancelled before publication"):
        embed_gguf_files(source, destination, {"new.bin": resource}, check_cancelled)
    assert not destination.exists()
    assert not list(tmp_path.glob(".output.gguf.*.tmp"))
