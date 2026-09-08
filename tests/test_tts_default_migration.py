from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.web.database import Database, sqlite_url, upgrade_database
from pandrator.web.models import (
    AppSetting,
    AppSettingHistory,
    OwnerAccount,
    SessionRecord,
    SessionSetting,
)
from pandrator.web.workspace import WorkspaceSettingsService


def old_database(path):
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "pandrator/web/migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(path))
    command.upgrade(config, "0043_quick_transcriptions")
    return Database(path)


def test_fresh_workspace_prefers_audio_cpp(tmp_path):
    path = tmp_path / "fresh.db"
    upgrade_database(path)
    database = Database(path)
    try:
        with database.session() as session:
            record = SessionRecord(name="Fresh")
            session.add(record)
            session.flush()
            session_id = record.id
        assert (
            WorkspaceSettingsService(database).get(session_id, "tts")["effective"][
                "service"
            ]
            == "audio_cpp"
        )
    finally:
        database.dispose()


@pytest.mark.parametrize(
    "defaults", [None, {"speed": 1.2}, {"service": "kokoro", "voice": "af_heart"}]
)
def test_upgrade_preserves_existing_defaults_and_session_overrides(tmp_path, defaults):
    path = tmp_path / "old.db"
    database = old_database(path)
    try:
        with database.session() as session:
            session.add(OwnerAccount(singleton_id=1, password_hash="unchanged"))
            record = SessionRecord(name="Existing")
            session.add(record)
            session.flush()
            session_id = record.id
            inherited = SessionRecord(name="Inherited")
            session.add(inherited)
            session.flush()
            inherited_id = inherited.id
            untouched = {
                "service": "FishS2",
                "model": "s2-pro",
                "voice": "my-voice",
                "fishs2_top_p": 0.6,
            }
            session.add(
                SessionSetting(
                    session_id=session_id,
                    section="tts",
                    value_json=untouched,
                    revision=3,
                )
            )
            if defaults is not None:
                session.add(
                    AppSetting(key="defaults.tts", value_json=defaults, revision=7)
                )
        upgrade_database(path)
        with database.session() as session:
            global_record = session.get(AppSetting, "defaults.tts")
            assert global_record.value_json == {
                **(defaults or {}),
                "service": (defaults or {}).get("service", "XTTS"),
            }
            old = session.get(SessionSetting, (session_id, "tts"))
            assert old.value_json == untouched and old.revision == 3
            if defaults is not None and "service" not in defaults:
                prior = session.scalar(
                    select(AppSettingHistory).where(
                        AppSettingHistory.key == "defaults.tts"
                    )
                )
                assert prior.value_json == defaults and prior.revision == 7
                assert global_record.revision == 8
        assert WorkspaceSettingsService(database).get(inherited_id, "tts")["effective"][
            "service"
        ] == (defaults or {}).get("service", "XTTS")
        upgrade_database(path)
        assert (
            WorkspaceSettingsService(database).get(session_id, "tts")["effective"][
                "service"
            ]
            == "FishS2"
        )
    finally:
        database.dispose()
