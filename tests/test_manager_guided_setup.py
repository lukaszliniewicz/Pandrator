import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pandrator.web.api import create_app as create_web_app
from pandrator.web.auth import ALL_SCOPES, BootstrapTokenStore
from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.components.runtime_bootstrap import generated_runtime_files
from pandrator_manager.components.slots import component_pointer
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.models import (
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
)
from pandrator_manager.runtime_specs import pandrator_runtime_specs
from pandrator_manager.supervisor import ProcessSupervisor
from tests.web_test_support import prepare_web_test_data_root


class GuidedCatalogueTests(unittest.TestCase):
    def test_catalogue_is_grouped_with_pandrator_first_and_typed_models(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            items = application.list_components()

        self.assertEqual("pandrator", items[0]["definition"]["id"])
        self.assertEqual("core", items[0]["definition"]["section"])

        qwen = next(
            item
            for item in items
            if item["definition"]["id"] == "qwen_tts"
        )
        self.assertEqual("text_to_speech", qwen["definition"]["section"])
        self.assertTrue(qwen["definition"]["models"])
        self.assertEqual(
            ["initial_model", "model_size", "quantization"],
            [
                option["key"]
                for option in qwen["definition"]["install_options"]
            ],
        )
        self.assertIn(
            "voice_cloning",
            {
                capability["id"]
                for capability in qwen["definition"]["capabilities"]
                if capability["available"]
            },
        )
        qwen_options = {
            option["key"]: option
            for option in qwen["definition"]["install_options"]
        }
        self.assertEqual("1.7b", qwen_options["model_size"]["default"])

        crisp = next(
            item
            for item in items
            if item["definition"]["id"] == "crispasr"
        )
        self.assertIn("install", crisp["definition"]["supported_actions"])
        self.assertEqual(3, len(crisp["definition"]["models"]))
        self.assertEqual("estimate", crisp["definition"]["size_provenance"])
        crisp_options = {
            option["key"]: option
            for option in crisp["definition"]["install_options"]
        }
        self.assertEqual(
            "moss-transcribe-diarize-0.9b",
            crisp_options["engine"]["default"],
        )
        self.assertEqual("q8_0", crisp_options["quantization"]["default"])

        kokoro = next(
            item
            for item in items
            if item["definition"]["id"] == "kokoro"
        )
        self.assertIn("estimate", kokoro["definition"]["size_note"].lower())
        self.assertIn("install", kokoro["definition"]["supported_actions"])
        voxcpm = next(
            item
            for item in items
            if item["definition"]["id"] == "voxcpm"
        )
        self.assertIn("install", voxcpm["definition"]["supported_actions"])
        self.assertEqual(
            ["cpu", "cuda"],
            voxcpm["definition"]["compute_variants"],
        )
        self.assertIn(
            "cuda_recommended",
            {
                item["id"]
                for item in voxcpm["definition"]["capabilities"]
                if item["available"]
            },
        )

    def test_manager_owned_runtime_adapters_are_valid_python(self):
        for component_id in ("kokoro", "voxcpm"):
            files = generated_runtime_files(component_id)
            self.assertIn("pandrator-manager-run.py", files)
            if component_id == "kokoro":
                self.assertIn(".pandrator-manager/pixi.toml", files)
            compile(
                files["pandrator-manager-run.py"],
                f"<{component_id}-manager-runner>",
                "exec",
            )
            if component_id == "kokoro":
                runner = files["pandrator-manager-run.py"]
                self.assertIn("PHONEMIZER_ESPEAK_DATA_PATH", runner)
                self.assertIn("EspeakWrapper.set_data_path", runner)
                self.assertIn("uvicorn.run(", runner)

    def test_combined_plan_supports_qwen_and_verified_crispasr_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "qwen_tts": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        quantization="q8_0",
                        options={
                            "initial_model": "base",
                            "model_size": "0.6b",
                        },
                    ),
                    "crispasr": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        quantization="f16",
                        options={"engine": "whisper-large-v3"},
                    ),
                },
            )
        self.assertIn("stage_crispasr", {task.kind for task in plan.tasks})
        self.assertEqual(
            {"crispasr", "qwen_tts"},
            set(plan.desired).intersection({"crispasr", "qwen_tts"}),
        )
        self.assertGreater(plan.estimated_download_bytes, 0)

    def test_qwen_rejects_custom_voice_with_the_unsupported_small_model(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            with self.assertRaisesRegex(ValueError, "not available"):
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired={
                        "qwen_tts": DesiredComponentState(
                            compute=ComputeVariant.CPU,
                            quantization="q8_0",
                            options={
                                "initial_model": "custom_voice",
                                "model_size": "0.6b",
                            },
                        )
                    },
                )


class ManagedApplicationLaunchTests(unittest.TestCase):
    @staticmethod
    def _activate_fixture(layout, component_id):
        source = layout.services / component_id / "versions" / "fixture"
        source.mkdir(parents=True)
        component_pointer(layout, component_id).write_text(
            json.dumps({"path": str(source)}),
            encoding="utf-8",
        )
        return source

    def test_failed_application_launch_is_typed_and_visible_in_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            application.instance_id = "guided-test"
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="guided-test",
            )
            secret = "m" * 43
            api = create_api(
                application,
                supervisor,
                client_secret=secret,
            )
            client = api.test_client()
            headers = {
                "Authorization": f"Bearer {secret}",
                "Idempotency-Key": "launch-absent",
            }
            response = client.post(
                "/v1/application/launch",
                headers=headers,
                json={},
            )
            self.assertEqual(409, response.status_code)
            self.assertEqual(
                "application_not_installed",
                response.get_json()["error"]["code"],
            )
            activity = client.get(
                "/v1/activity",
                headers={"Authorization": f"Bearer {secret}"},
            ).get_json()["items"]
            event_types = [event["event_type"] for event in activity]
            self.assertIn("application.action_requested", event_types)
            self.assertIn("application.action_failed", event_types)

    def test_frozen_manager_uses_pixi_not_its_launcher_as_python(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            source = layout.root / "app" / "versions" / "revision"
            (source / "pandrator" / "web").mkdir(parents=True)
            (source / "pyproject.toml").write_text(
                "[project]\nname='pandrator'\n",
                encoding="utf-8",
            )
            (source / "pixi.toml").write_text(
                "[workspace]\nname='pandrator'\n",
                encoding="utf-8",
            )
            (source / "pandrator" / "web" / "cli.py").write_text(
                "",
                encoding="utf-8",
            )
            component_pointer(layout, "pandrator").write_text(
                json.dumps({"path": str(source)}),
                encoding="utf-8",
            )
            pixi = layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
            pixi.write_bytes(b"pixi")

            with mock.patch.object(sys, "frozen", True, create=True):
                specs = pandrator_runtime_specs(layout)

        self.assertTrue(all(spec.executable == str(pixi) for spec in specs))
        self.assertTrue(all(spec.arguments[0] == "run" for spec in specs))
        self.assertTrue(
            all("-m" in spec.arguments and "pandrator" in spec.arguments for spec in specs)
        )

    def test_generic_runtime_start_refreshes_specs_after_first_install(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            application.instance_id = "guided-test"
            layout = application.context.layout
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="guided-test",
            )
            supervisor.register_many(pandrator_runtime_specs(layout))
            fallback = supervisor.spec("pandrator.api")
            self.assertIsNotNone(fallback)
            self.assertNotEqual(
                str(layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")),
                fallback.executable,
            )

            source = layout.root / "app" / "versions" / "revision"
            (source / "pandrator" / "web").mkdir(parents=True)
            (source / "pyproject.toml").write_text(
                "[project]\nname='pandrator'\n",
                encoding="utf-8",
            )
            (source / "pixi.toml").write_text(
                "[workspace]\nname='pandrator'\n",
                encoding="utf-8",
            )
            (source / "pandrator" / "web" / "cli.py").write_text(
                "",
                encoding="utf-8",
            )
            component_pointer(layout, "pandrator").write_text(
                json.dumps({"path": str(source)}),
                encoding="utf-8",
            )
            pixi = layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
            pixi.write_bytes(b"pixi")

            secret = "m" * 43
            api = create_api(
                application,
                supervisor,
                client_secret=secret,
            )
            client = api.test_client()

            def start_without_spawning(service_id):
                return next(
                    service
                    for service in supervisor.snapshot()
                    if service.id == service_id
                )

            with mock.patch.object(
                supervisor,
                "start",
                side_effect=start_without_spawning,
            ):
                response = client.post(
                    "/v1/runtime/start",
                    headers={
                        "Authorization": f"Bearer {secret}",
                        "Idempotency-Key": "start-after-first-install",
                    },
                    json={"service_ids": ["pandrator.api"]},
                )

            self.assertEqual(200, response.status_code)
            refreshed = supervisor.spec("pandrator.api")
            self.assertIsNotNone(refreshed)
            self.assertEqual(str(pixi), refreshed.executable)
            self.assertEqual("run", refreshed.arguments[0])
            self.assertIn("--locked", refreshed.arguments)

    def test_frozen_manager_uses_pixi_for_python_backend_bootstrappers(self):
        from pandrator_manager.models import ResolvedComponentState
        from pandrator_manager.runtime_specs import component_runtime_spec

        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            self._activate_fixture(layout, "qwen_tts")
            pixi = layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")
            resolved = ResolvedComponentState(
                compute=ComputeVariant.CPU,
                platform="test",
                options={"initial_model": "base"},
            )
            with mock.patch.object(sys, "frozen", True, create=True):
                spec = component_runtime_spec(layout, "qwen_tts", resolved)

        self.assertIsNotNone(spec)
        self.assertEqual(str(pixi), spec.executable)
        self.assertEqual("run", spec.arguments[0])
        self.assertIn("python", spec.arguments)
        self.assertIn("run.py", spec.arguments)
        self.assertIn("1.7b", spec.arguments)

    def test_qwen_runtime_spec_keeps_an_explicit_model_size(self):
        from pandrator_manager.models import ResolvedComponentState
        from pandrator_manager.runtime_specs import component_runtime_spec

        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            self._activate_fixture(layout, "qwen_tts")
            spec = component_runtime_spec(
                layout,
                "qwen_tts",
                ResolvedComponentState(
                    compute=ComputeVariant.CUDA,
                    platform="test",
                    quantization="f16",
                    options={"initial_model": "base", "model_size": "1.7b"},
                ),
            )

        self.assertIsNotNone(spec)
        model_size_index = spec.arguments.index("--model-size")
        self.assertEqual("1.7b", spec.arguments[model_size_index + 1])
        self.assertEqual("tcp", spec.readiness.kind)
        self.assertEqual(8042, spec.readiness.port)
        self.assertIsNone(spec.readiness.url)
        self.assertTrue(
            spec.environment["PANDRATOR_QWEN_STATE_DIR"].endswith(
                str(Path("state") / "services" / "qwen_tts")
            )
        )
        self.assertTrue(
            spec.environment["PANDRATOR_QWEN_MODELS_DIR"].endswith(
                str(Path("data") / "models" / "qwen_tts")
            )
        )
        self.assertTrue(
            spec.environment["PANDRATOR_QWEN_BIN_DIR"].endswith(
                str(Path("data") / "runtime" / "qwen_tts")
            )
        )

    def test_fish_runtime_spec_keeps_large_artifacts_outside_version_slot(self):
        from pandrator_manager.models import ResolvedComponentState
        from pandrator_manager.runtime_specs import component_runtime_spec

        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            self._activate_fixture(layout, "fish_speech")
            spec = component_runtime_spec(
                layout,
                "fish_speech",
                ResolvedComponentState(
                    compute=ComputeVariant.VULKAN,
                    platform="test",
                    quantization="q6_k",
                ),
            )
            with self.assertRaisesRegex(
                ValueError,
                "does not support quantization",
            ):
                component_runtime_spec(
                    layout,
                    "fish_speech",
                    ResolvedComponentState(
                        compute=ComputeVariant.VULKAN,
                        platform="test",
                        quantization="not-a-real-quant",
                    ),
                )

        self.assertIsNotNone(spec)
        self.assertEqual(
            str(layout.data / "models" / "fish_speech" / "s2-pro-q6_k.gguf"),
            spec.environment["FISHS2_MODEL_PATH"],
        )
        self.assertEqual(
            str(layout.data / "models" / "fish_speech" / "tokenizer.json"),
            spec.environment["FISHS2_TOKENIZER_PATH"],
        )
        self.assertEqual(
            str(layout.data / "runtime" / "fish_speech"),
            spec.environment["FISHS2_RUNTIME_DIR"],
        )
        self.assertEqual(
            str(layout.state / "services" / "fish_speech" / "voices"),
            spec.environment["FISHS2_VOICES_DIR"],
        )
        self.assertEqual(60 * 60, spec.startup_timeout_seconds)

    def test_kokoro_and_voxcpm_have_manager_owned_runtime_contracts(self):
        from pandrator_manager.models import ResolvedComponentState
        from pandrator_manager.runtime_specs import component_runtime_spec

        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            self._activate_fixture(layout, "kokoro")
            self._activate_fixture(layout, "voxcpm")
            kokoro = component_runtime_spec(
                layout,
                "kokoro",
                ResolvedComponentState(
                    compute=ComputeVariant.CPU,
                    platform="test",
                ),
            )
            voxcpm = component_runtime_spec(
                layout,
                "voxcpm",
                ResolvedComponentState(
                    compute=ComputeVariant.CPU,
                    platform="test",
                ),
            )

        self.assertIsNotNone(kokoro)
        self.assertEqual((8880,), kokoro.ports)
        self.assertTrue(
            any(
                Path(argument).name == "pixi.toml"
                for argument in kokoro.arguments
            )
        )
        self.assertEqual(
            str(layout.state / "services" / "kokoro"),
            kokoro.environment["PANDRATOR_KOKORO_STATE_DIR"],
        )
        model_argument = kokoro.arguments.index("--model-dir") + 1
        self.assertEqual(
            str(layout.data / "models" / "kokoro"),
            kokoro.arguments[model_argument],
        )
        self.assertEqual("tcp", kokoro.readiness.kind)
        self.assertEqual("127.0.0.1", kokoro.readiness.host)
        self.assertEqual(8880, kokoro.readiness.port)
        self.assertIsNotNone(voxcpm)
        self.assertEqual((8021,), voxcpm.ports)
        self.assertIn("pandrator-manager-run.py", voxcpm.arguments)
        self.assertEqual("8021", voxcpm.environment["VOXCPM_PORT"])
        self.assertEqual("cpu", voxcpm.environment["VOXCPM_DEVICE"])
        backend_argument = voxcpm.arguments.index("--backend") + 1
        self.assertEqual("cpu", voxcpm.arguments[backend_argument])


class ManagerBootstrapSecurityTests(unittest.TestCase):
    def test_browser_and_automation_bootstraps_have_distinct_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            credential = Path(directory) / "manager.secret"
            credential.write_text("manager-secret", encoding="utf-8")
            bootstrap = BootstrapTokenStore()
            with mock.patch.dict(
                os.environ,
                {
                    "PANDRATOR_MANAGER_CREDENTIAL": str(credential),
                    "PANDRATOR_MCP_BOOTSTRAP_EXTRA_SCOPES": "",
                },
                clear=False,
            ):
                app = create_web_app(
                    data_root=directory,
                    testing=True,
                    bootstrap_tokens=bootstrap,
                )
                try:
                    client = app.test_client()
                    self.assertEqual(
                        401,
                        client.post(
                            "/api/v1/auth/manager-bootstrap",
                            headers={"Authorization": "Bearer wrong"},
                        ).status_code,
                    )
                    self.assertEqual(
                        401,
                        client.post(
                            "/api/v1/auth/manager-browser-bootstrap",
                            headers={"Authorization": "Bearer wrong"},
                        ).status_code,
                    )
                    self.assertEqual(
                        401,
                        client.post(
                            "/api/v1/auth/manager-browser-bootstrap",
                            headers={
                                "Authorization": "Bearer manager-secret"
                            },
                            environ_overrides={"REMOTE_ADDR": "192.0.2.20"},
                        ).status_code,
                    )
                    self.assertEqual(
                        401,
                        client.post(
                            "/api/v1/auth/manager-bootstrap",
                            headers={"Authorization": "Bearer manager-secret"},
                            environ_overrides={"REMOTE_ADDR": "192.0.2.20"},
                        ).status_code,
                    )
                    self.assertEqual(
                        403,
                        client.post(
                            "/api/v1/auth/manager-bootstrap",
                            headers={
                                "Authorization": (
                                    "Bearer manager-secret"
                                )
                            },
                            json={"scopes": ["app.admin"]},
                        ).status_code,
                    )
                    issued = client.post(
                        "/api/v1/auth/manager-bootstrap",
                        headers={"Authorization": "Bearer manager-secret"},
                        json={
                            "scopes": [
                                "app.read",
                                "app.admin",
                                "app.credentials.write",
                            ]
                        },
                    )
                    self.assertEqual(200, issued.status_code)
                    self.assertEqual(
                        ["app.read"],
                        issued.get_json()["scopes"],
                    )
                    token = issued.get_json()["token"]
                    self.assertEqual(
                        200,
                        client.post(
                            "/api/v1/auth/bootstrap",
                            json={"token": token},
                        ).status_code,
                    )
                    principal = client.get(
                        "/api/v1/auth/status"
                    ).get_json()["principal"]
                    self.assertEqual(
                        "manager_bootstrap",
                        principal["kind"],
                    )
                    self.assertEqual(["app.read"], principal["scopes"])
                    self.assertEqual(
                        403,
                        client.get("/api/v1/credentials").status_code,
                    )
                    self.assertEqual(
                        401,
                        client.post(
                            "/api/v1/auth/bootstrap",
                            json={"token": token},
                        ).status_code,
                    )

                    browser_client = app.test_client()
                    browser_grant = browser_client.post(
                        "/api/v1/auth/manager-browser-bootstrap",
                        headers={"Authorization": "Bearer manager-secret"},
                    )
                    self.assertEqual(200, browser_grant.status_code)
                    self.assertEqual(
                        sorted(ALL_SCOPES),
                        browser_grant.get_json()["scopes"],
                    )
                    browser_exchange = browser_client.post(
                        "/api/v1/auth/bootstrap",
                        json={"token": browser_grant.get_json()["token"]},
                    )
                    self.assertEqual(200, browser_exchange.status_code)
                    browser_principal = browser_client.get(
                        "/api/v1/auth/status"
                    ).get_json()["principal"]
                    self.assertEqual("owner_session", browser_principal["kind"])
                    self.assertEqual("owner", browser_principal["subject"])
                    self.assertEqual(
                        "pandrator-manager-browser",
                        browser_principal["client_id"],
                    )
                    self.assertEqual(
                        sorted(ALL_SCOPES),
                        browser_principal["scopes"],
                    )
                    self.assertEqual(
                        200,
                        browser_client.get("/api/v1/credentials").status_code,
                    )
                    created = browser_client.post(
                        "/api/v1/providers",
                        headers={
                            "X-CSRF-Token": browser_exchange.get_json()[
                                "csrf_token"
                            ]
                        },
                        json={
                            "provider_key": "openai",
                            "label": "Browser-owned provider",
                        },
                    )
                    self.assertEqual(201, created.status_code)
                finally:
                    app.extensions["pandrator"]["database"].dispose()


if __name__ == "__main__":
    unittest.main()
