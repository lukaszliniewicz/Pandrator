"""Signed wheel activation with database/package rollback."""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    version: str
    wheel_name: str
    wheel_sha256: str


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_release_manifest(manifest_path: Path, public_key_path: Path) -> VerifiedRelease:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as error:  # pragma: no cover - packaging dependency
        raise RuntimeError("Signed updates require the cryptography package.") from error

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    signed = payload.get("signed") if isinstance(payload, dict) else None
    signature_text = str(payload.get("signature") or "") if isinstance(payload, dict) else ""
    if not isinstance(signed, dict) or not signature_text:
        raise ValueError("Release manifest must contain signed metadata and an Ed25519 signature.")
    key_bytes = public_key_path.read_bytes()
    try:
        key = serialization.load_pem_public_key(key_bytes)
    except ValueError:
        raw = base64.b64decode(key_bytes.strip(), validate=True)
        key = Ed25519PublicKey.from_public_bytes(raw)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("Release public key is not an Ed25519 key.")
    try:
        key.verify(base64.b64decode(signature_text, validate=True), _canonical(signed))
    except Exception as error:
        raise ValueError("Release manifest signature verification failed.") from error
    wheel = signed.get("wheel") if isinstance(signed.get("wheel"), dict) else {}
    name = str(wheel.get("filename") or "").strip()
    digest = str(wheel.get("sha256") or "").strip().lower()
    version = str(signed.get("version") or "").strip()
    if not version or not name or len(digest) != 64:
        raise ValueError("Signed release metadata is missing version or wheel identity fields.")
    return VerifiedRelease(version=version, wheel_name=name, wheel_sha256=digest)


def snapshot_sqlite(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as incoming, sqlite3.connect(destination) as backup:
        incoming.backup(backup)
    return True


def _site_packages(python: Path) -> Path:
    result = subprocess.run(
        [str(python), "-c", "import json,site; print(json.dumps(site.getsitepackages()))"],
        check=True,
        capture_output=True,
        text=True,
    )
    candidates = [Path(value) for value in json.loads(result.stdout)]
    return next((path.resolve() for path in candidates if path.is_dir()), candidates[0].resolve())


def snapshot_installed_package(python: Path, destination: Path) -> Path:
    site_packages = _site_packages(python)
    members = [site_packages / "pandrator", site_packages / "pandrator_installer"]
    members.extend(site_packages.glob("pandrator-*.dist-info"))
    present = [item for item in members if item.exists()]
    if not present:
        raise RuntimeError(f"No installed Pandrator package was found in {site_packages}.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("snapshot.json", json.dumps({"site_packages": str(site_packages)}))
        for root in present:
            if root.is_file():
                archive.write(root, root.name)
            else:
                for path in root.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(site_packages).as_posix())
    return site_packages


def restore_installed_package(snapshot: Path, site_packages: Path) -> None:
    site_packages = site_packages.resolve()
    targets = [site_packages / "pandrator", site_packages / "pandrator_installer", *site_packages.glob("pandrator-*.dist-info")]
    for target in targets:
        resolved = target.resolve()
        if resolved.parent != site_packages:
            raise RuntimeError(f"Refusing to restore outside site-packages: {resolved}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
        elif resolved.exists():
            resolved.unlink()
    with zipfile.ZipFile(snapshot) as archive:
        for member in archive.infolist():
            if member.filename == "snapshot.json":
                continue
            destination = (site_packages / member.filename).resolve()
            if site_packages not in destination.parents:
                raise RuntimeError("Package snapshot contains an escaped path.")
            archive.extract(member, site_packages)


def install_wheel(python: Path, wheel: Path) -> None:
    subprocess.run(
        [str(python), "-m", "pip", "install", "--upgrade", "--force-reinstall", "--no-deps", str(wheel)],
        check=True,
    )


def run_migrations(python: Path, data_root: Path) -> None:
    subprocess.run([str(python), "-m", "pandrator", "--data-dir", str(data_root), "--json", "migrate"], check=True)


def health_check(python: Path, data_root: Path, timeout: float = 35.0) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    process = subprocess.Popen(
        [str(python), "-m", "pandrator", "--data-dir", str(data_root), "serve", "--host", "127.0.0.1", "--port", str(port), "--no-open-browser"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        start_new_session=os.name != "nt",
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Updated API exited during health check with code {process.returncode}.")
            try:
                with urlopen(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as response:
                    if response.status == 200 and json.loads(response.read()).get("status") == "ok":
                        return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("Updated API did not pass its health check.")
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def restore_database(snapshot: Path, destination: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        target = Path(str(destination) + suffix)
        if target.exists():
            target.unlink()
    if snapshot.is_file():
        shutil.copy2(snapshot, destination)
