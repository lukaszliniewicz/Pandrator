from types import SimpleNamespace
from unittest.mock import Mock

from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas import GetWorkInput
from pandrator_mcp.tools.e2e import _safe_tts_service
from pandrator_mcp.tools.work import get_work


def test_success_refreshes_catalogue_and_distinguishes_loaded_from_selectable():
    service = {
        "id": "audio_cpp",
        "available": True,
        "models": ["base", "design"],
        "model_catalog": [
            {"id": "base", "loaded": True},
            {"id": "design", "loaded": False},
            {"id": "uninstalled"},
        ],
    }
    application = Mock()
    application.tts_catalog.return_value = {"services": [service]}
    runtime = SimpleNamespace(
        application=application, require_application=lambda: application, manager=Mock()
    )
    runtime.manager.operation.return_value = {"id": "operation", "state": "succeeded"}
    result = get_work(
        runtime, GetWorkInput(work_type="manager_operation", work_id="operation")
    )
    application.tts_catalog.assert_called_once_with(refresh=True)
    states = result.result["tts_catalogue_refresh"]["services"][0]["model_states"]
    assert states == [
        {"id": "base", "selectable": True, "loaded": True},
        {"id": "design", "selectable": True, "loaded": False},
        {"id": "uninstalled", "selectable": False, "loaded": None},
    ]
    assert _safe_tts_service(service, model_ids=["design"])["model_states"] == [
        states[1]
    ]


def test_restart_during_refresh_does_not_hide_completed_operation():
    application = Mock()
    application.tts_catalog.side_effect = PandratorMcpError(
        "application_unavailable", "Restarting"
    )
    runtime = SimpleNamespace(
        application=application, require_application=lambda: application, manager=Mock()
    )
    runtime.manager.operation.return_value = {"id": "operation", "state": "succeeded"}
    result = get_work(
        runtime, GetWorkInput(work_type="manager_operation", work_id="operation")
    )
    assert result.work.state == "succeeded"
    assert result.result["tts_catalogue_refresh"]["status"] == "application_unavailable"
