import json
from sqlalchemy import create_engine, select
from pandrator.web.models import Artifact, SessionRecord
from pandrator.web.database import database_url
from pandrator.web.workspace import WorkspaceSettingsService, adapt_runtime_settings
import hashlib

def get_hash(d):
    return hashlib.sha256(json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()

engine = create_engine(database_url(None))

with engine.connect() as conn:
    results = conn.execute(select(Artifact.id, Artifact.session_id, Artifact.metadata_json, Artifact.settings_json, Artifact.settings_hash).where(Artifact.role == 'translation').order_by(Artifact.created_at.desc())).fetchall()
    if not results:
        print("No translation artifacts found.")
    else:
        for row in results[:3]:
            art_id, session_id, meta, settings_json, settings_hash = row
            print(f"Artifact {art_id}")
            meta = meta or {}
            print(f"Requested hash in meta: {meta.get('requested_settings_hash')}")
            
            ws = WorkspaceSettingsService(engine)
            resolved, _ = ws.resolve(session_id, ["translation", "subtitles"], {})
            
            stage_value = {}
            for section in ["translation", "subtitles"]:
                stage_value.update(adapt_runtime_settings(section, resolved.get(section, {})))
                
            expected_hash = get_hash(stage_value)
            print(f"Expected hash for generate_audio (stage_value): {expected_hash}")
            if expected_hash != meta.get('requested_settings_hash'):
                print("HASH MISMATCH!")
                print(f"Current stage_value: {json.dumps(stage_value, sort_keys=True)}")
                print(f"Hydrated settings_json: {json.dumps(settings_json, sort_keys=True)}")
                
            print("-" * 40)
