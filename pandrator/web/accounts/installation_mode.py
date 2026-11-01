"""Solo-first installation policy with qualified Administrator opt-in collaboration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from ..access.contracts import AccessContext, AccessDenied
from ..model_base import utcnow
from ..models import AppSetting
from .actor_authorization import require_actor_in_session
from .auth_models import UserCredential
from .auth_state import initial_user_id

INSTALLATION_POLICY_KEY = "auth.installation"
# Qualified feature availability; saved opt-in and Administrator password are still required.
# No environment, request or saved-setting bypass for a closed build.
COLLABORATION_AVAILABLE = True


class InstallationModeError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 409):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


@dataclass(frozen=True, slots=True)
class InstallationMode:
    mode: Literal["solo", "collaboration"]
    revision: int
    available: bool

    @property
    def enabled(self) -> bool:
        return self.available and self.mode == "collaboration"

    def payload(self) -> dict[str, str | int | bool]:
        return {
            "mode": self.mode,
            "revision": self.revision,
            "collaboration_available": self.available,
            "collaboration_enabled": self.enabled,
        }


def installation_mode_in_session(session: Session) -> InstallationMode:
    with session.no_autoflush:
        row = session.execute(
            select(AppSetting.value_json, AppSetting.revision).where(
                AppSetting.key == INSTALLATION_POLICY_KEY
            )
        ).one_or_none()
    if row is None:
        return InstallationMode("solo", 0, COLLABORATION_AVAILABLE)
    value = row.value_json
    if (
        type(value) is not dict
        or set(value) != {"schema_version", "mode"}
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or type(value.get("mode")) is not str
        or value["mode"] not in {"solo", "collaboration"}
        or type(row.revision) is not int
        or row.revision < 1
    ):
        raise InstallationModeError(
            "installation_policy_invalid", "The installation access policy needs repair."
        )
    return InstallationMode(value["mode"], row.revision, COLLABORATION_AVAILABLE)


def collaboration_enabled_clause():
    """Merge the persisted opt-in check into the existing account SELECT."""
    fields = func.json_each(AppSetting.value_json).table_valued("key", "value")
    field_count = select(func.count()).select_from(fields).correlate(AppSetting).scalar_subquery()
    return exists(
        select(AppSetting.key).where(
            AppSetting.key == INSTALLATION_POLICY_KEY,
            func.json_type(AppSetting.value_json) == "object",
            field_count == 2,
            func.json_type(AppSetting.value_json, "$.schema_version") == "integer",
            AppSetting.value_json["schema_version"].as_integer() == 1,
            AppSetting.value_json["mode"].as_string() == "collaboration",
            AppSetting.revision >= 1,
        )
    )


def enable_collaboration_in_session(
    session: Session, context: AccessContext, *, expected_revision: int
) -> InstallationMode:
    """Same guarded command for first-run setup and a later settings change."""
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    try:
        require_actor_in_session(
            session, context.actor,
            required_scopes={"app.admin", "app.write"}, require_admin=True,
        )
    except PermissionError:
        raise AccessDenied() from None
    current = installation_mode_in_session(session)
    if not current.available:
        raise InstallationModeError(
            "collaboration_not_available", "Collaboration is awaiting release qualification."
        )
    if type(expected_revision) is not int or expected_revision < 0:
        raise InstallationModeError(
            "invalid_revision", "An installation policy revision is required.", 400
        )
    if expected_revision != current.revision:
        raise InstallationModeError(
            "installation_revision_conflict", "The installation policy changed. Refresh it and try again."
        )
    if current.enabled:
        return current
    # A Manager-launched local browser is insufficient proof of a usable login.
    if not session.scalar(
        select(UserCredential.password_hash).where(
            UserCredential.user_id == initial_user_id(session),
            UserCredential.password_hash.is_not(None),
            UserCredential.password_hash != "",
        )
    ):
        raise InstallationModeError(
            "administrator_password_required", "Set the initial Administrator password before enabling collaboration."
        )
    setting = session.get(AppSetting, INSTALLATION_POLICY_KEY)
    if setting is None:
        setting = AppSetting(key=INSTALLATION_POLICY_KEY, revision=1)
        session.add(setting)
    else:
        setting.revision += 1
    setting.value_json = {"schema_version": 1, "mode": "collaboration"}
    setting.updated_at = utcnow()
    session.flush()
    return InstallationMode("collaboration", setting.revision, True)
