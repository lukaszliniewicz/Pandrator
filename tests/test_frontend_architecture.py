import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_SOURCE = ROOT / "web" / "src"
API_CORE = WEB_SOURCE / "lib" / "api.ts"
API_CLIENTS = (
    API_CORE,
    WEB_SOURCE / "lib" / "domain-api.ts",
    WEB_SOURCE / "lib" / "admin-api.ts",
)
SOURCE_SUFFIXES = {".svelte", ".ts"}


def frontend_sources():
    return sorted(
        path
        for path in WEB_SOURCE.rglob("*")
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
    )


def source(path):
    return path.read_text(encoding="utf-8")


def nested_keys(value, key):
    found = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for child in value.values():
            found.extend(nested_keys(child, key))
    elif isinstance(value, list):
        for child in value:
            found.extend(nested_keys(child, key))
    return found


def test_api_core_is_the_only_direct_network_gateway():
    offenders = []
    for path in frontend_sources():
        if path == API_CORE:
            continue
        text = source(path)
        if re.search(r"\bfetch\s*\(", text) or "XMLHttpRequest" in text:
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []
    assert source(API_CORE).count("fetch(") == 1


def test_api_core_assigns_idempotency_keys_to_mutations():
    core = source(API_CORE)
    assert "headers.has('Idempotency-Key')" in core
    assert "headers.set('Idempotency-Key', createIdempotencyKey())" in core
    assert "globalThis.crypto.randomUUID()" in core
    assert all(
        "randomUUID(" not in source(path)
        for path in frontend_sources()
        if path != API_CORE
    )


def test_legacy_catch_all_api_client_cannot_return():
    call = re.compile(r"\bapi\s*(?:<[^>]+>)?\s*\(")
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in frontend_sources()
        if call.search(source(path))
    ]
    assert offenders == []
    assert not re.search(r"export async function api\b", source(API_CORE))


def test_application_invalidation_does_not_use_window_custom_events():
    offenders = []
    for path in frontend_sources():
        text = source(path)
        if (
            "new CustomEvent(" in text
            or "window.dispatchEvent(" in text
            or "window.addEventListener(" in text
        ):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_typed_client_paths_exist_in_openapi():
    document = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    declared = set(document["paths"])
    literal_path = re.compile(r"""['"](/api/v1/[^'"]+)['"]""")
    requested = {
        match
        for client in API_CLIENTS
        for match in literal_path.findall(source(client))
    }
    assert requested
    assert requested - declared == set()


def test_openapi_templates_declare_path_parameters_without_nested_defs():
    document = json.loads((ROOT / "openapi.json").read_text(encoding="utf-8"))
    assert nested_keys(document, "$defs") == []
    for path, path_item in document["paths"].items():
        expected = set(re.findall(r"{([^}]+)}", path))
        for method, operation in path_item.items():
            if method not in {"get", "put", "post", "delete", "patch"}:
                continue
            inherited = path_item.get("parameters", [])
            parameters = inherited + operation.get("parameters", [])
            actual = {
                item["name"]
                for item in parameters
                if item.get("in") == "path" and item.get("required") is True
            }
            assert expected <= actual, f"{method.upper()} {path}"


def test_core_workflows_have_no_any_escape_hatches():
    core = (
        "lib/AddSourceDialog.svelte",
        "lib/GenerationDrawer.svelte",
        "lib/GlobalSettingsPanel.svelte",
        "lib/NewSessionWizard.svelte",
        "lib/OutputSettingsPanel.svelte",
        "lib/SessionWorkspace.svelte",
        "lib/SettingsPanel.svelte",
        "lib/SubtitleReview.svelte",
        "lib/TextOptimizationReview.svelte",
        "lib/WorkflowCustomizer.svelte",
        "lib/admin-api.ts",
        "lib/api-models.ts",
        "lib/app-state.svelte.ts",
        "lib/domain-api.ts",
        "lib/generation-store.svelte.ts",
        "lib/session-store.svelte.ts",
        "lib/workflow-store.svelte.ts",
        "routes/sessions/[id]/+layout.svelte",
        "routes/sessions/[id]/+page.svelte",
    )
    escape = re.compile(
        r"\bas\s+any\b|:\s*any\b|<\s*any\s*>|\bany\[\]|"
        r"Record<[^>]*\bany\b"
    )
    offenders = [
        relative
        for relative in core
        if escape.search(source(WEB_SOURCE / relative))
    ]
    assert offenders == []


def test_large_coordinators_delegate_presentation_and_avoid_transport():
    session_workspace = source(WEB_SOURCE / "lib" / "SessionWorkspace.svelte")
    generation_drawer = source(WEB_SOURCE / "lib" / "GenerationDrawer.svelte")
    for component in ("WorkflowStageCard", "WorkflowRunDialogs"):
        assert component in session_workspace
    for component in (
        "GenerationSegmentTable",
        "GenerationReadingView",
        "SpeechPlanReviewDialog",
    ):
        assert component in generation_drawer
    transport_import = re.compile(r"from\s+['\"](?:\$lib/|\./)api['\"]")
    assert not transport_import.search(session_workspace)
    assert not transport_import.search(generation_drawer)


def test_xtts_model_upload_is_exposed_by_source_and_compiled_shell():
    workspace = source(WEB_SOURCE / "lib" / "SessionWorkspace.svelte")
    api_client = source(WEB_SOURCE / "lib" / "domain-api.ts")
    static_root = ROOT / "pandrator" / "web" / "static"
    compiled = "\n".join(
        path.read_text(encoding="utf-8") for path in static_root.rglob("*.js")
    )

    assert "XTTS model management" in workspace
    assert "config.json" in workspace
    assert "uploadXttsModel" in workspace
    assert "/api/v1/services/tts/xtts/models" in api_client
    assert "/api/v1/services/tts/xtts/models" in compiled
    assert "XTTS model management" in compiled
    assert re.search(r"Upload and\s+select", compiled)
    assert "ttsModel = uploaded.id" in workspace
    assert "xtts_model: ttsModel" in workspace
