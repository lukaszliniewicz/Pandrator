import json
from pathlib import Path

from pandrator_manager.components.audiocpp import MODEL_PACKAGES
from pandrator_manager.components.builtin import AudioCppComponentDriver


def test_installed_models_require_active_config_and_package_files(tmp_path: Path):
    base = MODEL_PACKAGES['qwen3_tts_1_7b_base_q8_0']
    custom = MODEL_PACKAGES['qwen3_tts_1_7b_customvoice_q8_0']
    (tmp_path / 'server.json').write_text(json.dumps({'models': [{'id': base.id}, {'id': custom.id}]}))
    for path in base.required_paths(tmp_path / 'models'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'test package evidence')
    assert AudioCppComponentDriver.installed_model_ids(tmp_path) == (base.id,)
    (tmp_path / 'server.json').write_text(json.dumps({'models': [{'id': custom.id}]}))
    assert AudioCppComponentDriver.installed_model_ids(tmp_path) == ()


def test_unreadable_or_invalid_model_config_is_unknown(tmp_path: Path):
    assert AudioCppComponentDriver.installed_model_ids(tmp_path) is None
    for content in ('not json', '[]', '{"models": {}}'):
        (tmp_path / 'server.json').write_text(content)
        assert AudioCppComponentDriver.installed_model_ids(tmp_path) is None
