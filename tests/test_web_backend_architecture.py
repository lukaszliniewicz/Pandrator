import inspect
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pandrator.logic import tts_handler
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.domain_blueprints import DOMAIN_ORDER, route_domain
from pandrator.web.job_registry import JobHandlerRegistry, JobPayloadContract
from pandrator.web.tts_providers import (
    TtsBatchItem,
    TtsCapabilities,
    TtsHealth,
    TtsProviderAdapter,
    TtsProviderConfigurationError,
    TtsProviderError,
    TtsProviderRegistry,
    TtsRetryPolicy,
    _audio_cpp_static_model_catalog,
)


class BackendArchitectureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.app = create_app(
            data_root=cls.temporary.name,
            testing=True,
            bootstrap_tokens=BootstrapTokenStore(),
        )

    @classmethod
    def tearDownClass(cls):
        cls.app.extensions["pandrator"]["database"].dispose()
        cls.temporary.cleanup()

    def test_application_factory_is_composition_only(self):
        source = inspect.getsource(create_app)
        self.assertLess(len(source.splitlines()), 100)
        self.assertNotIn("@app.", source)
        self.assertIn("ApplicationServices.build", source)
        self.assertIn("register_routes", source)

    def test_route_contract_is_partitioned_without_losing_rules(self):
        rules = list(self.app.url_map.iter_rules())
        self.assertEqual(308, len(rules))
        self.assertEqual(
            301,
            sum(rule.rule.startswith("/api/") for rule in rules),
        )
        self.assertTrue({
            "/api/v1/sessions/<session_id>/sources/subtitle-status",
            "/api/v1/sessions/<session_id>/sources/align-subtitles",
            "/api/v1/sessions/<session_id>/sources/adopt-subtitles",
            "/api/v1/sessions/<session_id>/generation-plan/revisions",
            "/api/v1/sessions/<session_id>/generation-plan/topology/batch",
            "/api/v1/sessions/<session_id>/generation-plan/history",
            "/api/v1/sessions/<session_id>/generation-plan/repair-batches/<batch_id>",
            "/api/v1/sessions/<session_id>/generation-plan/repair-batches/<batch_id>/undo",
            "/api/v1/services/models/catalogue",
            "/api/v1/voice-catalog",
            "/api/v1/voice-catalog/capabilities",
            "/api/v1/voice-catalog/metadata",
            "/api/v1/voice-collections",
            "/api/v1/voice-collections/<collection_id>",
            "/api/v1/voices/<voice_id>/samples/from-artifact",
            "/api/v1/translation-project-operations/<operation_id>/exports/manifest",
            "/api/v1/translation-project-operations/<operation_id>/exports/bundle",
            "/api/v1/sessions/<session_id>/subtitles/<stage>/passage-review",
            "/api/v1/artifacts/<artifact_id>/video-preview",
        }.issubset({rule.rule for rule in rules}))
        self.assertEqual(set(DOMAIN_ORDER), set(self.app.blueprints))
        self.assertEqual("library", route_domain("/api/v1/voice-catalog"))
        self.assertEqual("library", route_domain("/api/v1/voice-collections"))
        for rule in rules:
            if rule.endpoint == "static":
                continue
            expected_domain = route_domain(rule.rule)
            self.assertEqual(
                expected_domain,
                rule.endpoint.split(".", 1)[0],
                rule.rule,
            )

    def test_runtime_routes_cover_every_openapi_operation(self):
        runtime_operations = {
            (re.sub(r"<[^>]+>", "{}", rule.rule), method.lower())
            for rule in self.app.url_map.iter_rules()
            for method in rule.methods
            if method not in {"HEAD", "OPTIONS"}
        }
        document = self.app.test_client().get("/api/v1/openapi.json").get_json()
        for path, operations in document["paths"].items():
            for method in operations:
                normalized_path = re.sub(r"{[^}]+}", "{}", path)
                self.assertIn(
                    (normalized_path, method.lower()),
                    runtime_operations,
                )

    def test_generation_start_facade_forwards_arguments_and_recaptures_all_ports(self):
        from dataclasses import FrozenInstanceError, fields

        from pandrator.web import workflow_handlers
        from pandrator.web.workflow_generation_start import GenerationStartContext

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        names = (
            "database", "_resolve_input", "_generation_language",
            "_materialize_subtitle_generation_plan", "_store_generation_plan",
            "run_generation", "_secret_free_tts_settings",
        )
        self.assertEqual(names, tuple(field.name for field in fields(GenerationStartContext)))
        payload = {"settings": {"unchanged": []}}
        progress, cancel = lambda *_args: None, threading.Event()
        snapshot = {"tts": {"unchanged": []}}
        expected = {"result": []}
        contexts = []
        for explicit in (False, True):
            ports = {name: object() for name in names}
            instance_ports = {name: value for name, value in ports.items()
                              if name != "_secret_free_tts_settings"}
            with patch.multiple(handlers, **instance_ports), patch.object(
                workflow_handlers, "_secret_free_tts_settings", ports["_secret_free_tts_settings"]
            ), patch.object(workflow_handlers, "_start_run_reviewable_generation") as owner:
                owner.return_value = expected
                optional = (
                    {"resolved_snapshot": snapshot, "settings_hash": "selected-hash", "job_id": "job"}
                    if explicit else {}
                )
                self.assertIs(expected, handlers._run_reviewable_generation(
                    payload, progress, cancel, **optional
                ))
                owner.assert_called_once()
                context, sent_payload, sent_progress, sent_cancel = owner.call_args.args
                contexts.append(context)
                self.assertIs(payload, sent_payload)
                self.assertIs(progress, sent_progress)
                self.assertIs(cancel, sent_cancel)
                self.assertEqual(
                    optional or {"resolved_snapshot": None, "settings_hash": None, "job_id": None},
                    owner.call_args.kwargs,
                )
                if explicit:
                    self.assertIs(snapshot, owner.call_args.kwargs["resolved_snapshot"])
                for name, value in ports.items():
                    self.assertIs(value, getattr(context, name))
                self.assertFalse(hasattr(context, "__dict__"))
                with self.assertRaises(FrozenInstanceError):
                    context.database = object()
        self.assertIsNot(contexts[0], contexts[1])
        for name in names:
            self.assertIsNot(getattr(contexts[0], name), getattr(contexts[1], name))

    def test_extension_mapping_uses_the_composed_service_instances(self):
        extension = self.app.extensions["pandrator"]
        services = extension["services"]
        self.assertIs(services.database, extension["database"])
        self.assertIs(services.workflow_handlers, extension["workflow_handlers"])
        self.assertIs(services.tts_providers, extension["tts_providers"])
        self.assertIs(
            services.tts_providers,
            services.workflow_handlers.tts_providers,
        )

    def test_workflow_job_registry_has_domain_ownership_and_late_binding(self):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        registry = handlers.handler_registry
        self.assertEqual(38, len(registry))
        self.assertEqual(
            {
                "delivery",
                "generation",
                "media_edit",
                "source",
                "text",
                "transcription",
                "voice",
                "workflow",
            },
            {item.domain for item in registry.registrations()},
        )

        original = handlers.translate
        calls = []

        def replacement(payload, _progress, _cancel_event):
            calls.append(payload)
            return {"replacement": True}

        try:
            handlers.translate = replacement
            result = registry["dubbing.translate"](
                {
                    "session_id": "example",
                    "source_artifact_id": "source",
                },
                lambda *_: None,
                threading.Event(),
            )
        finally:
            handlers.translate = original
        self.assertEqual({"replacement": True}, result)
        self.assertEqual(
            [
                {
                    "session_id": "example",
                    "source_artifact_id": "source",
                }
            ],
            calls,
        )

    def test_job_registry_rejects_duplicate_or_unowned_handlers(self):
        registry = JobHandlerRegistry()

        def handler(_payload, _progress, _cancel_event):
            return {}

        registry.register("example", handler, domain="test")
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register("example", handler, domain="other")
        with self.assertRaisesRegex(ValueError, "needs a domain"):
            JobHandlerRegistry().register("example", handler, domain="")
        validated = JobHandlerRegistry()
        validated.register(
            "validated",
            handler,
            domain="test",
            payload_contract=JobPayloadContract(("session_id",)),
        )
        with self.assertRaisesRegex(ValueError, "session_id"):
            validated["validated"]({}, lambda *_: None, threading.Event())

    def test_voice_facades_capture_current_dependencies_and_forward_identity(self):
        from pandrator.web import workflow_handlers

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        payload, progress, cancel = {"unchanged": []}, lambda *_args: None, threading.Event()
        fields = {
            name: object() for name in ("database", "paths", "artifacts", "tts_providers", "manager_bridge")
        }
        fields.update(_resolve_input=lambda _id: None, _session_dir=lambda _id: None)
        def scale(callback, _start, _end):
            return callback
        for name in (
            "transcribe_voice", "normalize_voice_recording", "publish_voice", "unpublish_voice",
            "upload_rvc_model", "convert_with_rvc", "train_xtts",
        ):
            with self.subTest(handler=name), patch.multiple(handlers, **fields), patch.object(
                workflow_handlers, "_scaled_progress_callback", scale
            ), patch.object(workflow_handlers, f"_voice_{name}") as owner:
                expected = {"result": name}
                owner.return_value = expected
                self.assertIs(expected, getattr(handlers, name)(payload, progress, cancel))
                owner.assert_called_once()
                context, sent_payload, sent_progress, sent_cancel = owner.call_args.args
                self.assertIs(payload, sent_payload)
                self.assertIs(progress, sent_progress)
                self.assertIs(cancel, sent_cancel)
                for field, value in fields.items():
                    self.assertIs(value, getattr(context, field))
                self.assertIs(scale, context._scaled_progress_callback)
        # A later invocation must replace the context and capture replaced fields.
        with patch.object(workflow_handlers, "_voice_publish_voice") as owner:
            handlers.publish_voice(payload, progress, cancel)
            initial = owner.call_args.args[0]
            changed_database = object()
            with patch.object(handlers, "database", changed_database):
                handlers.publish_voice(payload, progress, cancel)
            latest = owner.call_args.args[0]
            self.assertIsNot(initial, latest)
            self.assertIs(changed_database, latest.database)

    def test_extracted_voice_registry_handlers_remain_late_bound(self):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        registrations = {
            "voice.transcribe": "transcribe_voice", "voice.normalize_recording": "normalize_voice_recording",
            "voice.publish": "publish_voice", "voice.unpublish": "unpublish_voice",
            "rvc.model.upload": "upload_rvc_model", "rvc.convert": "convert_with_rvc",
            "training.xtts": "train_xtts",
        }
        payload = {key: "test" for key in (
            "voice_id", "sample_artifact_id", "source_artifact_id", "service_id", "pth_artifact_id", "index_artifact_id", "training_id"
        )}
        progress, cancel = lambda *_args: None, threading.Event()
        for kind, name in registrations.items():
            with self.subTest(kind=kind), patch.object(handlers, name) as replacement:
                expected = {"late_bound": kind}
                replacement.return_value = expected
                self.assertIs(expected, handlers.handler_registry[kind](payload, progress, cancel))
                replacement.assert_called_once_with(payload, progress, cancel)

    def test_source_facades_capture_current_dependencies_and_forward_identity(self):
        from pandrator.web import workflow_handlers

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        payload, progress, cancel = {"unchanged": []}, lambda *_args: None, threading.Event()
        fields = {name: object() for name in ("database", "paths", "artifacts")}
        fields.update({name: lambda *_args, **_kwargs: None for name in (
            "_resolve_input", "_session_dir", "_operation_dir", "_session_record",
            "_store_generation_plan", "_validate_download_url",
        )})
        callbacks = {name: lambda *_args, **_kwargs: None for name in (
            "_scaled_progress_callback", "_fraction_message_callback", "_source_cleaning_progress_callback",
        )}
        for name in ("download_source_url", "reuse_source", "clean_source", "prepare_source_cleaning_dispatch", "prepare_text"):
            with self.subTest(handler=name), patch.multiple(handlers, **fields), patch.multiple(
                workflow_handlers, **callbacks
            ), patch.object(workflow_handlers, f"_source_{name}") as owner:
                expected = {"result": name}
                owner.return_value = expected
                self.assertIs(expected, getattr(handlers, name)(payload, progress, cancel))
                owner.assert_called_once()
                context, sent_payload, sent_progress, sent_cancel = owner.call_args.args
                self.assertIs(payload, sent_payload)
                self.assertIs(progress, sent_progress)
                self.assertIs(cancel, sent_cancel)
                for field, value in {**fields, **callbacks}.items():
                    self.assertIs(value, getattr(context, field))
                # A second call captures replacements rather than retaining a context.
                changed_database = object()
                with patch.object(handlers, "database", changed_database):
                    getattr(handlers, name)(payload, progress, cancel)
                latest = owner.call_args.args[0]
                self.assertIsNot(context, latest)
                self.assertIs(changed_database, latest.database)

    def test_extracted_source_registry_handlers_remain_late_bound(self):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        registrations = {
            "source.download_url": "download_source_url", "source.reuse": "reuse_source",
            "source.clean": "clean_source", "text.prepare": "prepare_text",
            "source.cleaning_dispatch.prepare": "prepare_source_cleaning_dispatch",
        }
        payload = {key: "test" for key in (
            "session_id", "url", "artifact_id", "source_artifact_id", "source_cleaning_dispatch_run_id",
        )}
        progress, cancel = lambda *_args: None, threading.Event()
        for kind, name in registrations.items():
            with self.subTest(kind=kind), patch.object(handlers, name) as replacement:
                expected = {"late_bound": kind}
                replacement.return_value = expected
                self.assertIs(expected, handlers.handler_registry[kind](payload, progress, cancel))
                replacement.assert_called_once_with(payload, progress, cancel)

    def test_generation_plan_store_captures_fresh_dependencies_and_forwards_identity(self):
        from pandrator.web import workflow_handlers

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        records, settings, caller_session = [{"text": "Narration."}], {"voice": "fixture"}, object()
        fields = {"database": object()}
        fields.update({name: lambda *_args, **_kwargs: None for name in (
            "_is_subtitle_generation_record", "_usable_language", "_optimization_text_hash",
        )})
        helpers = {name: lambda *_args, **_kwargs: None for name in (
            "_generation_segmentation_settings", "_secret_free_tts_settings", "_default_silence_after_ms",
        )}
        with patch.multiple(handlers, **fields), patch.multiple(workflow_handlers, **helpers), patch.object(
            workflow_handlers, "_store_generation_plan_impl"
        ) as owner:
            expected = ("revision", ["segment"])
            owner.return_value = expected
            result = handlers._store_generation_plan(
                "session", records, settings=settings, source_revision_id="source-revision",
                source_artifact_id="source-artifact", db_session=caller_session, force_new=True,
            )
            self.assertIs(expected, result)
            owner.assert_called_once()
            context, session_id, sent_records = owner.call_args.args
            self.assertEqual("session", session_id)
            self.assertIs(records, sent_records)
            self.assertIs(settings, owner.call_args.kwargs["settings"])
            self.assertIs(caller_session, owner.call_args.kwargs["db_session"])
            self.assertEqual({"settings": settings, "source_revision_id": "source-revision", "source_artifact_id": "source-artifact", "db_session": caller_session, "force_new": True}, owner.call_args.kwargs)
            for name, value in {**fields, **helpers}.items():
                self.assertIs(value, getattr(context, name))
            replacements = {name: object() for name in fields}
            later_helpers = {name: object() for name in helpers}
            with patch.multiple(handlers, **replacements), patch.multiple(workflow_handlers, **later_helpers):
                handlers._store_generation_plan("session", records, settings=settings)
            latest = owner.call_args.args[0]
            self.assertIsNot(context, latest)
            for name, value in {**replacements, **later_helpers}.items():
                self.assertIs(value, getattr(latest, name))
            self.assertEqual({"settings": settings, "source_revision_id": None, "source_artifact_id": None, "db_session": None, "force_new": False}, owner.call_args.kwargs)

    def test_generation_binding_facades_capture_current_ports_and_forward_identity(self):
        from pandrator.web import workflow_handlers

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        source, path, settings = object(), Path("unchanged.srt"), {"unchanged": []}
        records = [{"text": "Unchanged."}]
        fields = {name: object() for name in ("database", "artifacts")}
        fields.update({name: lambda *_args, **_kwargs: None for name in (
            "_session_record", "_resolve_input", "_operation_dir", "_latest_stage_input", "_usable_language",
            "_subtitle_speaker_map", "_subtitle_generation_records", "_store_generation_plan",
            "_generation_source_for_plan_refresh", "_generation_language", "_materialize_subtitle_generation_plan",
        )})
        helpers = {name: lambda *_args, **_kwargs: None for name in (
            "_structured_speaker", "_speech_block_settings", "_speech_block_generation_mode",
            "_next_available_path", "_generation_segmentation_settings",
        )}
        helpers["logger"] = object()
        cases = (
            ("_generation_language", "generation_language", ("session", source, settings), {}, "pl"),
            ("_subtitle_speaker_map", "subtitle_speaker_map", (source, path), {}, {7: "Alice"}),
            ("_subtitle_speaker_map", "subtitle_speaker_map", (source,), {}, {7: "Alice"}),
            ("_subtitle_generation_records", "subtitle_generation_records", (source, path, settings, "pl"), {"session_id": "session"}, (records, None, source)),
            ("_subtitle_generation_records", "subtitle_generation_records", (source, path, settings, "pl"), {}, (records, None, source)),
            ("_materialize_subtitle_generation_plan", "materialize_subtitle_generation_plan", ("session", source, path, settings, "pl"), {}, "revision"),
            ("_generation_source_for_plan_refresh", "generation_source_for_plan_refresh", ("session",), {}, source),
            ("refresh_generation_plan", "refresh_generation_plan", ("session", settings), {}, "revision"),
        )
        for name, owner_name, args, kwargs, expected in cases:
            # Retain the original facade while replacing the callbacks it captures.
            facade = getattr(handlers, name)
            with self.subTest(handler=name, kwargs=kwargs), patch.multiple(handlers, **fields), patch.multiple(
                workflow_handlers, **helpers
            ), patch.object(workflow_handlers, f"_binding_{owner_name}") as owner:
                owner.return_value = expected
                self.assertIs(expected, facade(*args, **kwargs))
                owner.assert_called_once()
                context, *sent_args = owner.call_args.args
                expected_args = list(args)
                if owner_name == "subtitle_speaker_map" and len(expected_args) == 1:
                    expected_args.append(None)
                for expected_arg, sent_arg in zip(expected_args, sent_args, strict=True):
                    self.assertIs(expected_arg, sent_arg)
                self.assertEqual({"session_id": kwargs.get("session_id")} if owner_name == "subtitle_generation_records" else {}, owner.call_args.kwargs)
                for field, value in fields.items():
                    self.assertIs(value, getattr(context, field))
                for field, value in helpers.items():
                    self.assertIs(value, getattr(context, "_logger" if field == "logger" else field))
                replacements = {field: object() for field in fields}
                later_helpers = {field: object() for field in helpers}
                with patch.multiple(handlers, **replacements), patch.multiple(workflow_handlers, **later_helpers):
                    facade(*args, **kwargs)
                latest = owner.call_args.args[0]
                self.assertIsNot(context, latest)
                for field, value in replacements.items():
                    self.assertIs(value, getattr(latest, field))
                for field, value in later_helpers.items():
                    self.assertIs(value, getattr(latest, "_logger" if field == "logger" else field))

    def test_tts_registry_exposes_and_dispatches_the_provider_protocol(self):
        class RecordingAdapter:
            service_id = "xtts"

            def __init__(self):
                self.calls = []

            def capabilities(self, service):
                return TtsCapabilities(dynamic_catalog=True)

            def health(self, service):
                return TtsHealth(True, True)

            def enrich_catalog(self, service, *, api_key=""):
                return {"models": ["recorded"]}

            def synthesize(self, text, settings, **options):
                self.calls.append((text, settings, options))
                return "audio"

            def upload_voice(
                self,
                wav_file_path,
                *,
                base_url,
                service,
                prompt_text=None,
                mode=None,
                voice_id=None,
                api_key="",
            ):
                return "voice-id"

            def delete_voice(
                self,
                voice_id,
                *,
                base_url,
                service,
                api_key="",
            ):
                return True

        registry = TtsProviderRegistry()
        adapter = RecordingAdapter()
        self.assertIsInstance(adapter, TtsProviderAdapter)
        registry.replace(adapter)
        result = registry.synthesize(
            "Hello",
            {"service": "XTTS"},
            max_attempts=2,
        )
        self.assertEqual("audio", result)
        self.assertEqual("Hello", adapter.calls[0][0])
        self.assertEqual(2, adapter.calls[0][2]["max_attempts"])
        policy = TtsRetryPolicy.from_settings(
            {"max_attempts": 100, "retry_max_delay_seconds": 0}
        )
        self.assertEqual(20, policy.max_attempts)
        self.assertEqual(90.0, policy.maximum_delay_seconds)

        def invalid_synthesis(_text, _settings, **_options):
            raise ValueError("invalid voice")

        adapter.synthesize = invalid_synthesis
        with self.assertRaises(TtsProviderConfigurationError) as raised:
            registry.synthesize("Hello", {"service": "XTTS"})
        self.assertFalse(raised.exception.retryable)

    def test_audio_cpp_adapter_is_selected_and_reuses_one_batch_session(self):
        registry = TtsProviderRegistry()
        base_settings = {
            "service": "Custom",
            "openai_audio_endpoint": "audio-cpp-experimental",
            "model": "fish-audio-s2-pro",
            "provider_configs": [
                {
                    "id": "audio-cpp-experimental",
                    "name": "audio.cpp",
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:8080",
                    "adapter": "audio_cpp",
                    "speech_path": "/v1/audio/speech",
                }
            ],
        }

        self.assertEqual("audio_cpp", registry.service_id_for_settings(base_settings))
        self.assertEqual(
            "xtts",
            registry.service_id_for_settings(
                {**base_settings, "preview_service_id": "xtts"}
            ),
        )
        capabilities = registry.synthesis_capabilities(base_settings)
        self.assertFalse(capabilities.batch_synthesis)
        self.assertFalse(capabilities.streaming_batch)
        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            side_effect=["one", "two"],
        ) as synthesize:
            results = list(
                registry.synthesize_batch(
                    [
                        TtsBatchItem("1", "First", dict(base_settings)),
                        TtsBatchItem("2", "Second", dict(base_settings)),
                    ],
                    batch_size=10,
                )
            )

        self.assertEqual(["one", "two"], [item.audio for item in results])
        first_session = synthesize.call_args_list[0].kwargs["request_session"]
        second_session = synthesize.call_args_list[1].kwargs["request_session"]
        self.assertIs(first_session, second_session)

        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            side_effect=["three", "four"],
        ) as synthesize_again:
            registry.synthesize("Third", dict(base_settings))
            registry.synthesize("Fourth", dict(base_settings))
        self.assertIs(
            first_session,
            synthesize_again.call_args_list[0].kwargs["request_session"],
        )
        self.assertIs(
            first_session,
            synthesize_again.call_args_list[1].kwargs["request_session"],
        )

        with patch(
            "pandrator.logic.tts_handler.text_to_audio",
            return_value=None,
        ):
            failed = list(
                registry.synthesize_batch(
                    [TtsBatchItem("failed", "No audio", dict(base_settings))],
                    batch_size=1,
                )
            )
        self.assertIsNone(failed[0].audio)
        self.assertIsNotNone(failed[0].error)
        self.assertTrue(failed[0].error.retryable)

    def test_audio_cpp_preserves_failure_details_and_retryability_in_single_and_batch(self):
        registry = TtsProviderRegistry()
        settings = {"service": "audio.cpp", "model": "voxcpm2_q8_0"}
        for retryable in (False, True):
            with self.subTest(retryable=retryable):
                failure = tts_handler.TtsGenerationError(
                    "Speech generation failed (audio.cpp / voxcpm2_q8_0, HTTP 500): explanation",
                    retryable=retryable,
                )
                with patch("pandrator.logic.tts_handler.text_to_audio", side_effect=failure):
                    with self.assertRaises(TtsProviderError) as caught:
                        registry.synthesize("Test.", settings)
                    results = list(registry.synthesize_batch(
                        [TtsBatchItem("one", "Test.", settings)], batch_size=1
                    ))
                self.assertEqual(str(failure), str(caught.exception))
                self.assertEqual(retryable, caught.exception.retryable)
                self.assertEqual(str(failure), str(results[0].error))
                self.assertEqual(retryable, results[0].error.retryable)

    def test_audio_cpp_catalogue_uses_configured_models_and_model_voices(self):
        registry = TtsProviderRegistry()
        service = {
            "id": "audio-cpp-experimental",
            "adapter": "audio_cpp",
            "api_base": "http://127.0.0.1:8088",
            "models_path": "/v1/models",
            "voices_path": "/v1/audio/voices",
            "default_model": "fish",
            "default_voice": "sofia",
            "voices": ["manual"],
        }
        with (
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                return_value=[
                    {"id": "fish", "family": "fish_audio", "task": "tts"},
                    {"id": "breeze", "family": "breeze_tts", "task": "clon"},
                    {"id": "omnivoice", "family": "omnivoice", "task": "clon"},
                ],
            ),
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                side_effect=[
                    ["narrator"],
                    ["breeze-voice"],
                    ["narrator", "reader"],
                ],
            ),
        ):
            catalogue = registry.enrich_catalog(service)

        self.assertEqual(["fish", "breeze", "omnivoice"], catalogue["models"])
        self.assertEqual(
            {
                "fish": ["sofia", "manual", "narrator"],
                "breeze": ["manual", "breeze-voice"],
                "omnivoice": ["manual", "narrator", "reader"],
            },
            catalogue["voice_catalogues"],
        )
        self.assertEqual(
            ["sofia", "manual", "narrator", "breeze-voice", "reader"],
            catalogue["voices"],
        )
        self.assertEqual("optional_cloning", catalogue["model_voice_modes"]["breeze"])
        self.assertEqual("fish", catalogue["default_model"])

    def test_audio_cpp_live_catalogue_excludes_models_not_configured_in_server(self):
        registry = TtsProviderRegistry()
        service = {
            "id": "audio_cpp",
            "adapter": "audio_cpp",
            "api_base": "http://127.0.0.1:8080",
            "default_model": "not-installed",
            "model_catalog": [
                {"id": "installed", "family": "fish_audio"},
                {"id": "not-installed", "family": "qwen3_tts"},
            ],
        }
        with (
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                return_value=[{"id": "installed", "task": "tts"}],
            ),
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                return_value=[],
            ),
        ):
            catalogue = registry.enrich_catalog(service)

        self.assertEqual(["installed"], catalogue["models"])
        self.assertEqual("installed", catalogue["default_model"])

    def test_audio_cpp_live_qwen_catalogue_keeps_static_segment_recommendation(self):
        registry = TtsProviderRegistry()
        service = {
            "id": "audio_cpp",
            "adapter": "audio_cpp",
            "api_base": "http://127.0.0.1:8060",
            "model_catalog": [{"id": "qwen3_tts_1_7b_base_q8_0"}],
        }
        with (
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_model_catalog",
                return_value=[
                    {
                        "id": "qwen3_tts_1_7b_base_q8_0",
                        "task": "tts",
                    }
                ],
            ),
            patch(
                "pandrator.logic.tts_handler.get_audio_cpp_voice_catalog",
                return_value=[],
            ),
        ):
            catalogue = registry.enrich_catalog(service)

        self.assertEqual(
            300,
            catalogue["model_catalog"][0]["recommended_chunk_characters"],
        )

    def test_audio_cpp_static_catalogue_refreshes_metadata_without_network(self):
        catalogue = _audio_cpp_static_model_catalog(
            {
                "models": ["qwen3_tts_1_7b_base_q8_0"],
                "model_catalog": [{"id": "qwen3_tts_1_7b_base_q8_0"}],
            }
        )

        self.assertEqual(300, catalogue[0]["recommended_chunk_characters"])


if __name__ == "__main__":
    unittest.main()
