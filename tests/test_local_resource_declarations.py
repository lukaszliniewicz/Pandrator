"""Queue declarations for local execution resources."""

import socket

import pytest

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.generation_scheduling import generation_resource_keys
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, SessionSource, SourceAsset
from pandrator.web.resource_keys import canonical_resource_key, tts_resource_keys
from pandrator.web.sessions import SessionService
from pandrator.web.stt_resources import stt_resource_keys
from pandrator.web.workflows import WorkflowService
from tests.web_test_support import prepare_web_test_data_root


def _generation_tts_resources(settings):
    return generation_resource_keys("fixture-session", {"tts": settings})[1:]


def _workflow_tts_resources(settings):
    return WorkflowService._resource_keys("fixture-session", "generate_audio", settings)[1:]


@pytest.mark.parametrize(
    "declaration", [_generation_tts_resources, _workflow_tts_resources]
)
def test_local_and_api_tts_resource_sets_are_disjoint(declaration):
    local = declaration({"service": "audio_cpp", "compute_backend": "auto"})
    api = declaration({"service": "openai", "compute_backend": "cuda"})

    assert local == ["service:tts:audio_cpp", "gpu:default"]
    assert api == ["service:tts:openai"]
    assert set(local).isdisjoint(api)


@pytest.mark.parametrize(
    "declaration", [_generation_tts_resources, _workflow_tts_resources]
)
def test_local_backend_labels_share_default_gpu_resource(declaration):
    cuda = declaration({"service": "audio_cpp", "compute_backend": "cuda"})
    vulkan = declaration({"service": "audio_cpp", "device": "vulkan"})

    assert cuda == ["service:tts:audio_cpp", "gpu:default"]
    assert vulkan == cuda


@pytest.mark.parametrize(
    "declaration", [_generation_tts_resources, _workflow_tts_resources]
)
def test_explicit_cpu_local_tts_does_not_claim_gpu(declaration):
    keys = declaration({"service": "audio_cpp", "compute_backend": " CPU "})

    assert keys == ["service:tts:audio_cpp"]
    assert set(keys).isdisjoint({"gpu:default"})


@pytest.mark.parametrize(
    "declaration", [_generation_tts_resources, _workflow_tts_resources]
)
def test_cloud_tts_ignores_stale_local_compute_setting(declaration):
    assert declaration({"service": "openai", "compute_backend": "cuda"}) == [
        "service:tts:openai"
    ]


def test_unknown_tts_service_keeps_legacy_identity_without_locality():
    assert tts_resource_keys({}) == ["service:tts:tts"]
    assert tts_resource_keys(
        {"service": "unlisted-endpoint", "compute_backend": "cuda"}
    ) == ["service:tts:unlisted_endpoint"]


@pytest.mark.parametrize(
    "alias",
    [
        "gpu:default",
        "gpu:0",
        "gpu:auto",
        "gpu:best",
        "gpu:cuda",
        "gpu:vulkan",
        "gpu:metal",
        "gpu:gpu",
        "gpu:mps",
        "gpu:rocm",
        "gpu:hip",
        "gpu:dml",
    ],
)
def test_gpu_aliases_share_default_resource(alias):
    assert canonical_resource_key(alias) == "gpu:default"


def test_service_aliases_and_unrelated_resource_bytes_are_canonicalized():
    assert canonical_resource_key(" service:tts:audio.cpp ") == "service:tts:audio_cpp"
    assert canonical_resource_key("service:tts:Qwen3") == "service:tts:kobold_qwen"
    assert canonical_resource_key(" voice:MixedCase ") == "voice:MixedCase"


def test_explicit_gpu_device_resource_stays_distinct():
    assert canonical_resource_key("gpu:1") == "gpu:1"
    assert canonical_resource_key("gpu:2") == "gpu:2"
    assert canonical_resource_key("gpu:1") != canonical_resource_key("gpu:2")


@pytest.mark.parametrize(
    "backend", ["cuda", "vulkan", "metal", "gpu", "mps", "rocm", "hip", "dml"]
)
def test_other_workflow_backends_use_default_gpu_resource(backend):
    keys = WorkflowService._resource_keys(
        "fixture-session", "correct", {"compute_backend": backend}
    )

    assert keys == ["session:fixture-session", "service:llm", "gpu:default"]


def test_video_hardware_export_claims_gpu_but_cpu_export_does_not():
    hardware = WorkflowService._resource_keys(
        "fixture-session",
        "export",
        {"export_mode": "media", "burn_video_encoder": "h264_nvenc"},
    )
    cpu = WorkflowService._resource_keys(
        "fixture-session",
        "export",
        {
            "export_mode": "media",
            "burn_video_encoder": "libx264",
            "compute_backend": "cuda",
        },
    )

    assert hardware == ["session:fixture-session", "gpu:default"]
    assert cpu == ["session:fixture-session"]


def test_video_book_hardware_export_claims_gpu():
    keys = WorkflowService._resource_keys(
        "fixture-session",
        "export",
        {"export_mode": "video_book", "burn_video_encoder": "hevc_qsv"},
    )

    assert keys == ["session:fixture-session", "gpu:default"]


@pytest.mark.parametrize("mode", ["audio", "subtitles", "text"])
def test_non_video_export_modes_ignore_hardware_encoder_setting(mode):
    keys = WorkflowService._resource_keys(
        "fixture-session",
        "export",
        {"export_mode": mode, "burn_video_encoder": "h264_nvenc"},
    )

    assert keys == ["session:fixture-session"]


def test_workflow_transcription_uses_conservative_stt_resources():
    automatic = WorkflowService._resource_keys("fixture-session", "transcribe", {})
    cpu = WorkflowService._resource_keys(
        "fixture-session", "transcribe", {"stt_compute_backend": "cpu"}
    )
    mixed_native = WorkflowService._resource_keys(
        "fixture-session",
        "transcribe",
        {
            "stt_engine": "qwen3",
            "stt_compute_backend": "cpu",
            "qwen_asr_backend": "vulkan",
        },
    )

    assert automatic == ["session:fixture-session", "service:stt", "gpu:default"]
    assert cpu == ["session:fixture-session", "service:stt"]
    assert mixed_native == [
        "session:fixture-session",
        "service:stt",
        "service:tts:audio_cpp",
        "gpu:default",
    ]


def test_tts_catalogue_metadata_lookup_does_not_connect_to_network(monkeypatch):
    def deny_network(*_args, **_kwargs):
        raise AssertionError("resource metadata lookup attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(socket.socket, "connect", deny_network)

    from pandrator.logic import tts_service_catalogue

    get_service_config = tts_service_catalogue.get_service_config
    projected_fields = []

    def record_projection(*args, **kwargs):
        projected_fields.append(kwargs.get("_fields"))
        return get_service_config(*args, **kwargs)

    monkeypatch.setattr(tts_service_catalogue, "get_service_config", record_projection)
    assert tts_resource_keys(
        {"service": "audio_cpp", "compute_backend": "auto"}
    ) == ["service:tts:audio_cpp", "gpu:default"]
    assert projected_fields == [frozenset({"id", "kind"})]


def test_plain_stt_defaults_to_gpu_and_explicit_cpu_does_not():
    assert stt_resource_keys({}) == ["service:stt", "gpu:default"]
    assert stt_resource_keys({"compute_backend": "cpu"}) == ["service:stt"]


def test_implicit_continuation_transcription_claims_stt_resources(tmp_path, monkeypatch):
    from pandrator.logic import sentence_segmenter

    monkeypatch.setattr(sentence_segmenter, "_SEGMENTER", None)
    monkeypatch.setattr(sentence_segmenter, "_SEGMENTER_FAILED", True)

    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        record = SessionService(database).create(
            "Implicit transcription resource witness", workflow_kind="voiceover"
        )
        source_path = paths.uploads / "source.wav"
        source_path.write_bytes(b"synthetic source")
        source = ArtifactService(database, paths).register(
            source_path,
            kind="audio",
            role="upload",
            session_id=record.id,
        )
        with database.session() as session:
            row = session.get_one(Artifact, source.id)
            asset = SourceAsset(
                workspace_id=row.workspace_id,
                artifact_id=row.id,
                display_name="source.wav",
                kind="audio",
                state="current",
            )
            session.add(asset)
            session.flush()
            session.add(
                SessionSource(
                    session_id=record.id,
                    source_asset_id=asset.id,
                    role="primary",
                    is_current=True,
                )
            )

        resolved = WorkflowService(database, JobQueue(database)).resolve_stage(
            record.id, "generate_audio"
        )
        assert "service:stt" in resolved.resource_keys
        assert "gpu:default" in resolved.resource_keys
    finally:
        database.dispose()
