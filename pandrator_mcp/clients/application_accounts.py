"""Fixed-route application requests for account administration."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

from .application_requests import ApplicationRequests

_LIST_FIELDS = frozenset()
_CREATE_FIELDS = frozenset({"login_name", "display_name", "role", "idempotency_key"})
_UPDATE_FIELDS = frozenset({"user_id", "expected_auth_revision", "changes", "idempotency_key"})
_RESET_FIELDS = frozenset({"user_id", "expected_auth_revision"})
_CHANGE_FIELDS = frozenset({"display_name", "role", "is_active"})
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
_MAX_REQUEST_BYTES = 16 * 1024


def _arguments(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if type(action) is not str:
        raise ValueError("Unknown account action.")
    if not isinstance(arguments, dict):
        raise ValueError("Account request arguments must be an object.")
    fields = {
        "list": _LIST_FIELDS,
        "create": _CREATE_FIELDS,
        "update": _UPDATE_FIELDS,
        "issue_reset": _RESET_FIELDS,
    }.get(action)
    if fields is None:
        raise ValueError("Unknown account action.")
    values = dict(arguments)
    if set(values) - fields:
        raise ValueError("Account requests contain unsupported fields.")
    return values


def _required(values: dict[str, Any], fields: frozenset[str]) -> None:
    if not fields.issubset(values):
        raise ValueError("Account requests are missing required fields.")


def _string(value: Any, field: str, *, maximum: int, nonblank: bool = False) -> str:
    if type(value) is not str or len(value) > maximum:
        raise ValueError(f"Account request {field} must be a bounded string.")
    if nonblank and not value.strip():
        raise ValueError(f"Account request {field} must not be blank.")
    return value


def _user_id(value: Any) -> str:
    return _string(value, "user_id", maximum=64, nonblank=True)


def _revision(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("Account requests require a positive expected_auth_revision.")
    return value


def _idempotency_key(value: Any) -> str:
    if type(value) is not str or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError("Account writes require an 8-200 character idempotency_key.")
    return value


def _request_body(body: dict[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Account request values must be finite JSON.") from error
    if len(encoded) > _MAX_REQUEST_BYTES:
        raise ValueError("Account request bodies may not exceed 16 KiB.")
    return body


def _create_body(values: dict[str, Any]) -> dict[str, Any]:
    _required(values, frozenset({"login_name", "display_name", "idempotency_key"}))
    login_name = _string(values["login_name"], "login_name", maximum=4096)
    display_name = _string(values["display_name"], "display_name", maximum=4096)
    role = values.get("role", "user")
    if type(role) is not str or role not in {"user", "administrator"}:
        raise ValueError("Account role must be user or administrator.")
    return _request_body({"login_name": login_name, "display_name": display_name, "role": role})


def _update_body(values: dict[str, Any]) -> tuple[str, int, str, dict[str, Any]]:
    _required(values, _UPDATE_FIELDS)
    user_id = _user_id(values["user_id"])
    revision = _revision(values["expected_auth_revision"])
    key = _idempotency_key(values["idempotency_key"])
    raw_changes = values["changes"]
    if not isinstance(raw_changes, dict) or not raw_changes:
        raise ValueError("Account updates require at least one change.")
    changes = dict(raw_changes)
    if set(changes) - _CHANGE_FIELDS:
        raise ValueError("Account updates contain unsupported fields.")
    if any(value is None for value in changes.values()):
        raise ValueError("Account update fields cannot be null.")
    if "display_name" in changes:
        changes["display_name"] = _string(changes["display_name"], "display_name", maximum=4096)
    if "role" in changes:
        role = changes["role"]
        if type(role) is not str or role not in {"user", "administrator"}:
            raise ValueError("Account role must be user or administrator.")
    if "is_active" in changes and type(changes["is_active"]) is not bool:
        raise ValueError("Account is_active must be a boolean.")
    body = _request_body({"expected_auth_revision": revision, **changes})
    return user_id, revision, key, body


class ApplicationAccountMethods(ApplicationRequests):
    """Bounded request adapter for fixed account-administration routes."""

    def account_request(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        values = _arguments(action, arguments)

        if action == "list":
            if values:
                raise ValueError("Account list requests do not accept filters.")
            return self._request_json(
                "/api/v1/admin/users",
                read_only_operation=True,
            )

        if action == "create":
            body = _create_body(values)
            key = _idempotency_key(values["idempotency_key"])
            return self._request_json(
                "/api/v1/admin/users",
                method="POST",
                body=body,
                idempotency_key=key,
                maximum_body_bytes=_MAX_REQUEST_BYTES,
            )

        if action == "update":
            user_id, _revision_value, key, body = _update_body(values)
            return self._request_json(
                f"/api/v1/admin/users/{quote(user_id, safe='')}",
                method="PATCH",
                body=body,
                idempotency_key=key,
                maximum_body_bytes=_MAX_REQUEST_BYTES,
            )

        _required(values, _RESET_FIELDS)
        user_id = _user_id(values["user_id"])
        revision = _revision(values["expected_auth_revision"])
        return self._request_json(
            f"/api/v1/admin/users/{quote(user_id, safe='')}/password-reset",
            method="POST",
            body=_request_body({"expected_auth_revision": revision}),
            maximum_body_bytes=_MAX_REQUEST_BYTES,
        )


__all__ = ["ApplicationAccountMethods"]
