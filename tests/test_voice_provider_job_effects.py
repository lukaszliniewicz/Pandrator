"""Provider receipt storage migration and safe bounded projections."""

from __future__ import annotations

import gzip
import json
import sqlite3
from copy import deepcopy

import pytest
from alembic import command

from pandrator.web.database import SCHEMA_HEAD
from pandrator.web.models import Job
from pandrator.web.voice_provider_effects import voice_provider_effect_summary
from tests.test_workspace_invitation_migration import FIXTURE, config

PREVIOUS = "0083_workspace_invitations"
TARGET = "0084_job_external_effects"


def receipt():
    return {
        "version": 1,
        "operation": "voice.publish",
        "job_id": "job",
        "workspace_id": "workspace",
        "voice_id": "voice",
        "service_id": "xtts",
        "token": "a" * 64,
        "lease_generation": 1,
        "request_sha256": "b" * 64,
        "baseline_sha256": "c" * 64,
        "state": "completed",
        "binding_state": "not_applied",
        "provider_voice_id": "opaque/provider:id?with=syntax",
    }


def triggers(db):
    return db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()


def test_fresh_and_genuine_predecessor_upgrade_preserve_rows_and_triggers(tmp_path):
    old = tmp_path / "genuine-0083.sqlite3"
    with sqlite3.connect(old) as db:
        db.executescript(gzip.decompress(FIXTURE.read_bytes()).decode())
    command.upgrade(config(old), PREVIOUS)
    with sqlite3.connect(old) as db:
        assert "external_effect_json" not in {
            row[1] for row in db.execute("PRAGMA table_info(jobs)")
        }
        workspace = db.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]
        db.execute(
            """INSERT INTO jobs
          (id,kind,workspace_id,status,payload_json,resource_keys_json,progress,lease_generation,
           attempts,max_attempts,created_at,updated_at)
          VALUES ('retained-job','voice.publish',?,'failed','{}','[]',0,1,1,1,'2030-01-01','2030-01-01')""",
            (workspace,),
        )
        rows = db.execute("SELECT * FROM jobs").fetchall()
        old_triggers = triggers(db)
        assert old_triggers
    command.upgrade(config(old), TARGET)
    fresh = tmp_path / "fresh.sqlite3"
    command.upgrade(config(fresh), "head")
    assert SCHEMA_HEAD == TARGET
    assert Job.__table__.c.external_effect_json.nullable
    assert Job.__table__.c.external_effect_json.default is None
    assert Job.__table__.c.external_effect_json.type.none_as_null
    for path in (old, fresh):
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET,)
            shape = {row[1]: row for row in db.execute("PRAGMA table_info(jobs)")}
            assert shape["external_effect_json"][3:5] == (0, None)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(old) as db:
        assert db.execute("SELECT * FROM jobs").fetchall() == [(*row, None) for row in rows]
        assert triggers(db) == old_triggers
        db.execute(
            "UPDATE jobs SET external_effect_json=? WHERE id='retained-job'",
            (json.dumps(receipt()),),
        )
    with pytest.raises(RuntimeError, match="Cannot discard retained provider effect"):
        command.downgrade(config(old), PREVIOUS)
    with sqlite3.connect(old) as db:
        assert (
            json.loads(db.execute("SELECT external_effect_json FROM jobs").fetchone()[0])
            == receipt()
        )
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == (TARGET,)
        db.execute("UPDATE jobs SET external_effect_json=NULL")
    command.downgrade(config(old), PREVIOUS)
    with sqlite3.connect(old) as db:
        assert db.execute("SELECT * FROM jobs").fetchall() == rows
        assert triggers(db) == old_triggers
        assert "external_effect_json" not in {
            row[1] for row in db.execute("PRAGMA table_info(jobs)")
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_summary_whitelists_opaque_ids_without_private_receipt_fields():
    value = receipt()
    assert voice_provider_effect_summary(value) == {
        key: value[key]
        for key in ("operation", "state", "service_id", "provider_voice_id", "binding_state")
    }
    assert voice_provider_effect_summary(None) is None
    assert voice_provider_effect_summary({}) is None
    assert voice_provider_effect_summary({**value, "unexpected": "secret"}) is None
    assert voice_provider_effect_summary({**value, "provider_voice_id": "x" * 1025}) is None
    assert voice_provider_effect_summary({**value, "provider_voice_id": "bad\nvoice"}) is None
    assert voice_provider_effect_summary({**value, "lease_generation": True}) is None
    altered = deepcopy(value)
    altered.update(binding_state="ready", handler_result={"arbitrary": "response body"})
    assert voice_provider_effect_summary(altered) is None
