"""Domain-specific durable job registrations."""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING

from .job_registry import JobHandlerRegistry, JobPayloadContract
from .jobs import JobHandler

if TYPE_CHECKING:
    from .workflow_handlers import WorkflowHandlers


def _late_bound(
    handlers: WorkflowHandlers,
    method_name: str,
) -> JobHandler:
    """Resolve the method at dispatch time so test/runtime overrides remain valid."""

    original = getattr(handlers, method_name)

    @wraps(original)
    def dispatch(payload, progress, cancel_event):
        return getattr(handlers, method_name)(payload, progress, cancel_event)

    return dispatch


def _bind_many(
    handlers: WorkflowHandlers,
    registrations: dict[str, str],
) -> dict[str, JobHandler]:
    return {
        kind: _late_bound(handlers, method_name)
        for kind, method_name in registrations.items()
    }


def _contracts(
    fields_by_kind: dict[str, tuple[str, ...]],
) -> dict[str, JobPayloadContract]:
    return {
        kind: JobPayloadContract(required_fields=fields)
        for kind, fields in fields_by_kind.items()
    }


def register_text_handlers(
    registry: JobHandlerRegistry,
    handlers: WorkflowHandlers,
) -> None:
    registry.register_many(
        "text",
        _bind_many(
            handlers,
            {
                "dubbing.transcribe": "transcribe",
                "dubbing.correct": "correct",
                "dubbing.translate": "translate",
                "text.optimize_tts": "optimize_tts",
                "source.clean": "clean_source",
                "text.prepare": "prepare_text",
                "subtitle.evidence": "run_subtitle_evidence",
            },
        ),
        payload_contracts=_contracts(
            {
                "dubbing.transcribe": ("session_id", "source_artifact_id"),
                "dubbing.correct": ("session_id", "source_artifact_id"),
                "dubbing.translate": ("session_id", "source_artifact_id"),
                "text.optimize_tts": ("session_id", "source_artifact_id"),
                "source.clean": ("session_id", "source_artifact_id"),
                "text.prepare": ("session_id", "source_artifact_id"),
                "subtitle.evidence": ("evidence_id", "session_id"),
            }
        ),
    )


def register_generation_handlers(
    registry: JobHandlerRegistry,
    handlers: WorkflowHandlers,
) -> None:
    registry.register_many(
        "generation",
        _bind_many(
            handlers,
            {
                "dubbing.generate_audio": "generate_dubbing_audio",
                "audiobook.generate_audio": "generate_audiobook_audio",
                "generation.run": "run_generation",
                "generation.assemble": "assemble_generation_output",
                "audio.waveform": "generate_waveform",
                "audio.preview": "generate_audio_preview",
                "tts.preview": "preview_tts_voice",
            },
        ),
        payload_contracts=_contracts(
            {
                "dubbing.generate_audio": (
                    "session_id",
                    "source_artifact_id",
                ),
                "audiobook.generate_audio": (
                    "session_id",
                    "source_artifact_id",
                ),
                "generation.run": ("generation_run_id",),
                "generation.assemble": ("output_assembly_id",),
                "audio.waveform": ("source_artifact_id",),
                "audio.preview": ("source_artifact_id",),
                "tts.preview": ("text", "settings"),
            }
        ),
    )


def register_voice_handlers(
    registry: JobHandlerRegistry,
    handlers: WorkflowHandlers,
) -> None:
    registry.register_many(
        "voice",
        _bind_many(
            handlers,
            {
                "voice.transcribe": "transcribe_voice",
                "voice.normalize_recording": "normalize_voice_recording",
                "voice.publish": "publish_voice",
                "voice.unpublish": "unpublish_voice",
                "rvc.model.upload": "upload_rvc_model",
                "rvc.convert": "convert_with_rvc",
                "training.xtts": "train_xtts",
            },
        ),
        payload_contracts=_contracts(
            {
                "voice.transcribe": ("voice_id", "sample_artifact_id"),
                "voice.normalize_recording": (
                    "voice_id",
                    "source_artifact_id",
                ),
                "voice.publish": ("voice_id", "service_id"),
                "voice.unpublish": ("voice_id", "service_id"),
                "rvc.model.upload": (
                    "pth_artifact_id",
                    "index_artifact_id",
                ),
                "rvc.convert": ("source_artifact_id",),
                "training.xtts": ("training_id", "source_artifact_id"),
            }
        ),
    )


def register_source_handlers(
    registry: JobHandlerRegistry,
    handlers: WorkflowHandlers,
) -> None:
    registry.register_many(
        "source",
        _bind_many(
            handlers,
            {
                "source.download_url": "download_source_url",
                "source.reuse": "reuse_source",
                "source.cleaning_dispatch.prepare": "prepare_source_cleaning_dispatch",
            },
        ),
        payload_contracts=_contracts(
            {
                "source.download_url": ("session_id", "url"),
                "source.reuse": ("session_id", "artifact_id"),
                "source.cleaning_dispatch.prepare": (
                    "session_id",
                    "source_artifact_id",
                    "source_cleaning_dispatch_run_id",
                ),
            }
        ),
    )


def register_delivery_handlers(
    registry: JobHandlerRegistry,
    handlers: WorkflowHandlers,
) -> None:
    registry.register_many(
        "delivery",
        _bind_many(
            handlers,
            {
                "export.create": "export",
                "export.variant": "export_variant",
                "output.mix_preview": "preview_output_mix",
                "pdf.apply_edits": "apply_pdf_edits",
                "session.bundle.export": "export_session_bundle",
                "session.bundle.import": "import_session_bundle",
            },
        ),
        payload_contracts=_contracts(
            {
                "export.create": ("session_id",),
                "export.variant": ("session_id",),
                "output.mix_preview": (
                    "session_id",
                    "source_artifact_id",
                    "dubbing_artifact_id",
                ),
                "pdf.apply_edits": ("source_artifact_id",),
                "session.bundle.export": ("session_id",),
                "session.bundle.import": ("source_artifact_id",),
            }
        ),
    )


def register_workflow_handlers(
    registry: JobHandlerRegistry,
    handlers: WorkflowHandlers,
) -> None:
    registry.register(
        "workflow.continue",
        _late_bound(handlers, "continue_workflow"),
        domain="workflow",
        payload_contract=JobPayloadContract(required_fields=("session_id",)),
    )


def build_workflow_handler_registry(
    handlers: WorkflowHandlers,
) -> JobHandlerRegistry:
    registry = JobHandlerRegistry()
    register_text_handlers(registry, handlers)
    register_generation_handlers(registry, handlers)
    register_voice_handlers(registry, handlers)
    register_source_handlers(registry, handlers)
    register_delivery_handlers(registry, handlers)
    register_workflow_handlers(registry, handlers)
    return registry
