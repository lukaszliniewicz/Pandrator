import sys
import json
import hashlib
from sqlalchemy import create_engine, select
from pandrator.web.models import Artifact, SessionRecord
from pandrator.web.database import database_url

def get_hash(d):
    return hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

engine = create_engine(database_url(None))

with engine.connect() as conn:
    results = conn.execute(select(Artifact.role, Artifact.settings_hash, Artifact.metadata_json, Artifact.settings_json).where(Artifact.role == 'translation')).fetchall()
    for row in results:
        role, shash, meta, sjson = row
        print(f"Role: {role}")
        print(f"Settings hash in DB: {shash}")
        print(f"Requested hash in meta: {meta.get('requested_settings_hash')}")
        print(f"Computed hash on settings_json: {get_hash(sjson)}")
