"""Safe ZIP/TAR extraction with containment and resource limits."""

from __future__ import annotations

import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


class SafeExtractor:
    def __init__(
        self,
        *,
        maximum_entries: int = 200_000,
        maximum_uncompressed_bytes: int = 64 * 1024 * 1024 * 1024,
    ) -> None:
        self.maximum_entries = int(maximum_entries)
        self.maximum_uncompressed_bytes = int(maximum_uncompressed_bytes)

    @staticmethod
    def _member_target(destination: Path, name: str) -> Path:
        normalized = PurePosixPath(str(name).replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError(f"Archive contains an unsafe path: {name}")
        target = (destination / Path(*normalized.parts)).resolve(strict=False)
        root = destination.resolve(strict=False)
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError(f"Archive contains an unsafe path: {name}") from None
        return target

    def extract(self, archive: Path, destination: Path) -> Path:
        archive = archive.expanduser().resolve()
        destination = destination.expanduser().resolve(strict=False)
        destination.mkdir(parents=True, exist_ok=True)
        if zipfile.is_zipfile(archive):
            self._extract_zip(archive, destination)
        elif tarfile.is_tarfile(archive):
            self._extract_tar(archive, destination)
        else:
            raise ValueError("Unsupported archive format.")
        return destination

    def _extract_zip(self, archive: Path, destination: Path) -> None:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            if len(members) > self.maximum_entries:
                raise ValueError("Archive contains too many entries.")
            total = sum(max(0, member.file_size) for member in members)
            if total > self.maximum_uncompressed_bytes:
                raise ValueError("Archive uncompressed size exceeds the limit.")
            for member in members:
                target = self._member_target(destination, member.filename)
                unix_mode = member.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ValueError("Archive symbolic links are not permitted.")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as input_handle, target.open("wb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
                permissions = unix_mode & 0o777
                if permissions:
                    target.chmod(permissions)

    def _extract_tar(self, archive: Path, destination: Path) -> None:
        with tarfile.open(archive, mode="r:*") as source:
            members = source.getmembers()
            if len(members) > self.maximum_entries:
                raise ValueError("Archive contains too many entries.")
            total = sum(max(0, member.size) for member in members if member.isfile())
            if total > self.maximum_uncompressed_bytes:
                raise ValueError("Archive uncompressed size exceeds the limit.")
            for member in members:
                target = self._member_target(destination, member.name)
                if member.issym() or member.islnk() or member.isdev():
                    raise ValueError("Archive links and device entries are not permitted.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise ValueError("Archive contains an unsupported entry type.")
                target.parent.mkdir(parents=True, exist_ok=True)
                input_handle = source.extractfile(member)
                if input_handle is None:
                    raise ValueError(f"Could not read archive member: {member.name}")
                with input_handle, target.open("wb") as output_handle:
                    shutil.copyfileobj(input_handle, output_handle, length=1024 * 1024)
                target.chmod(member.mode & 0o777)
