"""Multipart and response contracts for the first-party XTTS model wrapper."""

from __future__ import annotations

import os
import secrets
from collections.abc import Iterator
from typing import Any, Self
from urllib.parse import quote, urlsplit

import requests

XTTS_MODEL_BUNDLE_FILENAMES = (
    "config.json",
    "model.pth",
    "speakers_xtts.pth",
    "vocab.json",
)

_XTTS_MODEL_BUNDLE_FILENAME_SET = frozenset(XTTS_MODEL_BUNDLE_FILENAMES)

_XTTS_MODEL_UPLOAD_CHUNK_BYTES = 1024 * 1024

_XTTS_MODEL_UPLOAD_TIMEOUT_SECONDS = 60 * 60


def _xtts_uploaded_file_size(uploaded_file: Any) -> int:
    """Return a spooled multipart file's size without materializing its body."""

    stream = uploaded_file.stream
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
    except (AttributeError, OSError, ValueError) as error:
        raise ValueError(
            f"Could not stream '{uploaded_file.filename}' to the XTTS service."
        ) from error
    if size < 1:
        raise ValueError(f"'{uploaded_file.filename}' must not be empty.")
    return int(size)


class _SizedMultipartStream:
    """Expose a single-pass multipart iterator with a known content length."""

    def __init__(self, chunks: Iterator[bytes], content_length: int):
        self._chunks = chunks
        self._content_length = content_length

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> bytes:
        return next(self._chunks)

    def __len__(self) -> int:
        return self._content_length


def _xtts_model_bundle_upload_parts(
    model_id: str,
    uploaded_files: list[Any],
) -> tuple[str, Iterator[bytes]]:
    """Build a bounded-memory multipart stream for the first-party wrapper."""

    boundary = f"----PandratorXtts{secrets.token_hex(16)}"
    field_part = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="model_id"\r\n\r\n{model_id}\r\n'
    ).encode()
    file_parts: list[tuple[bytes, Any, int]] = []
    total_size = len(field_part)
    content_types = {
        "config.json": "application/json",
        "model.pth": "application/octet-stream",
        "speakers_xtts.pth": "application/octet-stream",
        "vocab.json": "application/json",
    }
    for uploaded_file in uploaded_files:
        filename = str(uploaded_file.filename)
        header = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {content_types[filename]}\r\n\r\n"
        ).encode()
        size = _xtts_uploaded_file_size(uploaded_file)
        file_parts.append((header, uploaded_file, size))
        total_size += len(header) + size + len(b"\r\n")
    final_part = f"--{boundary}--\r\n".encode("ascii")
    total_size += len(final_part)

    def stream() -> Iterator[bytes]:
        yield field_part
        for header, uploaded_file, _size in file_parts:
            yield header
            file_stream = uploaded_file.stream
            file_stream.seek(0)
            while chunk := file_stream.read(_XTTS_MODEL_UPLOAD_CHUNK_BYTES):
                yield chunk
            yield b"\r\n"
        yield final_part

    return boundary, _SizedMultipartStream(stream(), total_size)


def _xtts_models_endpoint(base_url: str, model_id: str = "") -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The configured XTTS endpoint is invalid.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The configured XTTS endpoint is invalid.")
    base = str(base_url).strip().rstrip("/")
    if parsed.path.rstrip("/").endswith("/v1"):
        base = base[: -len("/v1")]
    endpoint = f"{base}/v1/models"
    if model_id:
        # Model identifiers are relative slash paths in the first-party XTTS
        # wrapper.  Preserve their hierarchy while escaping every path part.
        endpoint = f"{endpoint}/{quote(model_id, safe='/')}"
    return endpoint


def _xtts_health_endpoint(base_url: str) -> str:
    return _xtts_models_endpoint(base_url).removesuffix("/v1/models") + "/health"


def _xtts_model_id_error(model_id: str) -> str:
    """Apply only transport-safety checks; the wrapper owns full validation."""

    if not model_id or model_id != model_id.strip():
        return "model_id must be a non-empty relative identifier without surrounding spaces."
    if len(model_id) > 512:
        return "model_id is too long."
    if "\\" in model_id or model_id.startswith("/"):
        return "model_id must be a relative slash-separated path."
    parts = model_id.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
        return "model_id contains an unsafe path part."
    if any(ord(character) < 32 for character in model_id):
        return "model_id must not contain control characters."
    return ""


def _xtts_service_endpoint_base(catalogue: dict[str, Any]) -> tuple[str, str | None]:
    xtts_service = next(
        (
            service
            for service in catalogue.get("services", [])
            if str(service.get("id") or "").strip().lower() == "xtts"
        ),
        None,
    )
    if not isinstance(xtts_service, dict):
        return "", "The XTTS service is not configured."
    endpoint_base = str(xtts_service.get("api_base") or "").strip()
    if str(xtts_service.get("connection_mode") or "") == "managed_local":
        managed = xtts_service.get("manager_service")
        endpoint_base = (
            str(managed.get("endpoint") or "").strip() if isinstance(managed, dict) else ""
        )
        if not endpoint_base:
            return (
                "",
                "Pandrator Manager has not provided an XTTS endpoint for model management.",
            )
    try:
        # Validate here once, so all lifecycle proxy routes produce the same
        # connection error rather than constructing an unsafe outbound URL.
        _xtts_models_endpoint(endpoint_base)
    except ValueError as error:
        return "", str(error)
    return endpoint_base, None


def _xtts_lifecycle_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or "").strip()
    if not model_id:
        return None
    lifecycle_supported = any(
        key in item
        for key in (
            "is_default",
            "is_local",
            "removable",
            "source",
            "relative_path",
            "bundle_complete",
        )
    )
    created = item.get("created")
    if isinstance(created, bool):
        created = 0
    try:
        created_at = max(0, int(created or 0))
    except (OverflowError, TypeError, ValueError):
        created_at = 0
    return {
        "id": model_id,
        "object": str(item.get("object") or "model"),
        "created": created_at,
        "owned_by": str(item.get("owned_by") or "xtts-fapi"),
        "is_default": bool(item.get("is_default")) if lifecycle_supported else False,
        "is_local": bool(item.get("is_local")) if lifecycle_supported else False,
        "removable": bool(item.get("removable")) if lifecycle_supported else False,
        "source": str(item.get("source") or ("local" if lifecycle_supported else "unknown")),
        "relative_path": item.get("relative_path")
        if isinstance(item.get("relative_path"), str)
        else None,
        "bundle_complete": bool(item.get("bundle_complete")) if lifecycle_supported else True,
        "lifecycle_supported": lifecycle_supported,
    }


def _xtts_wrapper_error_details(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _xtts_wrapper_delete_unsupported(response: requests.Response) -> bool:
    """Recognize only missing route implementations, never missing models."""

    if response.status_code == 405:
        return True
    if response.status_code != 404:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict) or isinstance(payload.get("error"), dict):
        return False
    # FastAPI/Starlette's unimplemented-route response. A wrapper lifecycle
    # error uses the OpenAI-style ``error`` envelope and must stay actionable.
    return str(payload.get("detail") or "").strip() == "Not Found"


def _xtts_wrapper_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "The XTTS model service rejected the upload."
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:1000]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:1000]
    if isinstance(detail, list):
        messages = [
            str(item.get("msg") or "").strip()
            for item in detail
            if isinstance(item, dict) and str(item.get("msg") or "").strip()
        ]
        if messages:
            return "; ".join(messages[:5])[:1000]
    return "The XTTS model service rejected the upload."
