"""HTTP record projections shared across route domains."""

from typing import Any

from .credentials import redact_inline_secrets


def model_payload(record, fields: tuple[str, ...]) -> dict[str, Any]:
    payload = {field: getattr(record, field) for field in fields}
    for key, value in list(payload.items()):
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


def job_payload(record) -> dict[str, Any]:
    return redact_inline_secrets(
        model_payload(
            record,
            (
                "id",
                "kind",
                "session_id",
                "workflow_run_id",
                "status",
                "payload_json",
                "result_json",
                "progress",
                "progress_detail",
                "error_code",
                "error_message",
                "attempts",
                "max_attempts",
                "created_at",
                "started_at",
                "finished_at",
                "updated_at",
            ),
        )
    )
