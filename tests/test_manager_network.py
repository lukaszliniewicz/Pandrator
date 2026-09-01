import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from pandrator.runtime import DataPaths
from pandrator.web.capabilities import crispasr_install_preferences
from pandrator.web.cli import command_serve, serve_network_profile
from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.network import (
    AccessMode,
    EndpointExposure,
    NetworkConfiguration,
    load_network_configuration,
    save_network_configuration,
)
from pandrator_manager.runtime_specs import pandrator_runtime_specs
from pandrator_manager.supervisor import ProcessSupervisor


class NetworkConfigurationTests(unittest.TestCase):
    def test_loopback_is_the_default_and_remote_profiles_fail_closed(self):
        local = NetworkConfiguration()
        self.assertEqual(AccessMode.LOCAL, local.manager.mode)
        self.assertFalse(local.manager.remote_enabled)
        self.assertEqual("http://127.0.0.1:8097", local.application.browser_base_url)

        with self.assertRaisesRegex(ValueError, "public URL"):
            EndpointExposure(
                mode=AccessMode.PRIVATE_NETWORK,
                bind_host="0.0.0.0",
                port=8098,
                allow_insecure_remote=True,
            )
        with self.assertRaisesRegex(ValueError, "explicit"):
            EndpointExposure(
                mode=AccessMode.PRIVATE_NETWORK,
                bind_host="0.0.0.0",
                port=8098,
                public_url="http://gpu-box.local:8098",
            )
        with self.assertRaisesRegex(ValueError, "proxy hops"):
            EndpointExposure(
                mode=AccessMode.HTTPS_PROXY,
                bind_host="0.0.0.0",
                port=8098,
                public_url="https://gpu.example",
            )
        with self.assertRaisesRegex(ValueError, "must match"):
            EndpointExposure(
                mode=AccessMode.PRIVATE_NETWORK,
                bind_host="0.0.0.0",
                port=8098,
                public_url="http://gpu-box.local:8097",
                allow_insecure_remote=True,
            )

    def test_environment_profile_can_be_persisted_without_owner_password(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            environment = {
                "PANDRATOR_MANAGER_MODE": "private_network",
                "PANDRATOR_MANAGER_BIND_HOST": "0.0.0.0",
                "PANDRATOR_MANAGER_PORT": "8098",
                "PANDRATOR_MANAGER_PUBLIC_URL": "http://gpu-box.local:8098",
                "PANDRATOR_MANAGER_ALLOW_INSECURE_REMOTE": "true",
                "PANDRATOR_MODE": "https_proxy",
                "PANDRATOR_BIND_HOST": "0.0.0.0",
                "PANDRATOR_PORT": "8097",
                "PANDRATOR_PUBLIC_URL": "https://pandrator.example",
                "PANDRATOR_PROXY_HOPS": "1",
                "PANDRATOR_OWNER_PASSWORD": "must-not-be-persisted",
            }
            configured = load_network_configuration(
                layout,
                environment=environment,
            )
            save_network_configuration(layout, configured)
            payload = layout.network_configuration.read_text(encoding="utf-8")
            reloaded = load_network_configuration(layout, environment={})

        self.assertNotIn("must-not-be-persisted", payload)
        self.assertEqual("http://gpu-box.local:8098", reloaded.manager.public_url)
        self.assertEqual("https://pandrator.example", reloaded.application.public_url)
        self.assertTrue(reloaded.application.secure_cookies)

    def test_runtime_spec_uses_internal_probe_and_external_browser_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            crispasr = Path(directory) / "crispasr"
            crispasr.mkdir()
            crispasr_executable = crispasr / (
                "crispasr.exe" if os.name == "nt" else "crispasr"
            )
            crispasr_executable.write_bytes(b"fixture")
            exposure = EndpointExposure(
                mode=AccessMode.HTTPS_PROXY,
                bind_host="0.0.0.0",
                port=8097,
                public_url="https://pandrator.example",
                trusted_hosts=("alias.example",),
                proxy_hops=1,
            )
            with mock.patch(
                "pandrator_manager.runtime_specs.active_component_path",
                side_effect=lambda _layout, component_id: (
                    crispasr if component_id == "crispasr" else None
                ),
            ):
                api, mcp, worker = pandrator_runtime_specs(
                    layout,
                    exposure=exposure,
                    preferences={
                        "CRISPASR_DEFAULT_ENGINE": "moss",
                        "PANDRATOR_OWNER_PASSWORD": "one-time-owner-password",
                    },
                )

        self.assertIn("--public-url", api.arguments)
        self.assertIn("https://pandrator.example", api.arguments)
        self.assertIn("--proxy-hops", api.arguments)
        self.assertIn("pandrator.example", api.arguments)
        self.assertEqual(
            "http://127.0.0.1:8097/api/v1/health",
            api.readiness.url,
        )
        self.assertEqual("pandrator.mcp", mcp.service_id)
        self.assertEqual("http://127.0.0.1:8099/health", mcp.readiness.url)
        self.assertEqual(("pandrator.api",), mcp.dependencies)
        self.assertIn(str(layout.mcp_credential), mcp.arguments)
        self.assertIn(str(layout.mcp_configuration), mcp.arguments)
        self.assertEqual("moss", api.environment["CRISPASR_DEFAULT_ENGINE"])
        self.assertEqual(
            str(crispasr_executable),
            worker.environment["CRISPASR_EXECUTABLE"],
        )
        self.assertNotIn("PANDRATOR_OWNER_PASSWORD", worker.environment)

    def test_legacy_source_without_public_url_option_remains_launchable(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            legacy = layout.root / "Pandrator"
            cli = legacy / "pandrator" / "web" / "cli.py"
            cli.parent.mkdir(parents=True)
            cli.write_text(
                'serve.add_argument("--trusted-host")\n'
                'serve.add_argument("--allow-insecure-remote")\n',
                encoding="utf-8",
            )
            exposure = EndpointExposure(
                mode=AccessMode.PRIVATE_NETWORK,
                bind_host="0.0.0.0",
                port=8097,
                public_url="http://192.168.1.164:8097",
                trusted_hosts=("192.168.1.164",),
                allow_insecure_remote=True,
            )

            api, _worker = pandrator_runtime_specs(
                layout,
                exposure=exposure,
            )

        self.assertNotIn("--public-url", api.arguments)
        self.assertIn("--trusted-host", api.arguments)
        self.assertIn("--allow-insecure-remote", api.arguments)
        self.assertIsNone(api.readiness.expected_service)
        self.assertIsNone(api.readiness.expected_protocol)
        self.assertEqual({"status": "ok"}, api.readiness.expected_json)

    def test_crispasr_manager_preferences_override_legacy_config(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory)
            Path(directory, "config.json").write_text(
                '{"crispasr_engine":"whisper-large-v3",'
                '"crispasr_model_quantization":"f16"}',
                encoding="utf-8",
            )
            previous_engine = os.environ.get("CRISPASR_DEFAULT_ENGINE")
            previous_quantization = os.environ.get(
                "CRISPASR_DEFAULT_QUANTIZATION"
            )
            try:
                os.environ["CRISPASR_DEFAULT_ENGINE"] = "moss"
                os.environ["CRISPASR_DEFAULT_QUANTIZATION"] = "q4"
                preferences = crispasr_install_preferences(paths)
            finally:
                if previous_engine is None:
                    os.environ.pop("CRISPASR_DEFAULT_ENGINE", None)
                else:
                    os.environ["CRISPASR_DEFAULT_ENGINE"] = previous_engine
                if previous_quantization is None:
                    os.environ.pop("CRISPASR_DEFAULT_QUANTIZATION", None)
                else:
                    os.environ[
                        "CRISPASR_DEFAULT_QUANTIZATION"
                    ] = previous_quantization

        self.assertEqual("moss", preferences["engine"])
        self.assertEqual("q4_k", preferences["quantization"])


class RemoteRecoveryBoundaryTests(unittest.TestCase):
    def _api(self, directory, exposure):
        application = create_application(directory)
        application.instance_id = "network-test"
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id="network-test",
        )
        return create_api(
            application,
            supervisor,
            client_secret="n" * 43,
            manager_exposure=exposure,
        )

    def test_private_network_recovery_uses_public_url_and_exact_hosts(self):
        exposure = EndpointExposure(
            mode=AccessMode.PRIVATE_NETWORK,
            bind_host="0.0.0.0",
            port=8098,
            public_url="http://gpu-box.local:8098",
            allow_insecure_remote=True,
        )
        with tempfile.TemporaryDirectory() as directory:
            client = self._api(directory, exposure).test_client()
            remote = {"REMOTE_ADDR": "192.0.2.44"}
            self.assertEqual(
                200,
                client.get(
                    "/v1/health",
                    headers={"Host": "gpu-box.local:8098"},
                    environ_overrides=remote,
                ).status_code,
            )
            self.assertEqual(
                400,
                client.get(
                    "/v1/health",
                    headers={"Host": "attacker.example"},
                    environ_overrides=remote,
                ).status_code,
            )
            self.assertEqual(
                403,
                client.get(
                    "/v1/health",
                    headers={
                        "Host": "gpu-box.local:8098",
                        "Origin": "http://attacker.example",
                    },
                    environ_overrides=remote,
                ).status_code,
            )
            self.assertEqual(
                403,
                client.get(
                    "/v1/health",
                    headers={
                        "Host": "gpu-box.local:8098",
                        "Origin": "http://gpu-box.local:invalid",
                    },
                    environ_overrides=remote,
                ).status_code,
            )
            response = client.post(
                "/v1/recovery-sessions",
                headers={
                    "Host": "gpu-box.local:8098",
                    "Authorization": f"Bearer {'n' * 43}",
                    "Idempotency-Key": "remote-recovery",
                },
                json={},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(200, response.status_code)
        self.assertTrue(
            response.get_json()["url"].startswith(
                "http://gpu-box.local:8098/recovery#token="
            )
        )

    def test_https_proxy_sets_secure_cookie_and_rejects_spoofed_origin(self):
        exposure = EndpointExposure(
            mode=AccessMode.HTTPS_PROXY,
            bind_host="127.0.0.1",
            port=8098,
            public_url="https://setup.example",
            proxy_hops=1,
        )
        forwarded = {
            "Host": "127.0.0.1:8098",
            "X-Forwarded-For": "198.51.100.25",
            "X-Forwarded-Host": "setup.example",
            "X-Forwarded-Port": "443",
            "X-Forwarded-Proto": "https",
        }
        with tempfile.TemporaryDirectory() as directory:
            client = self._api(directory, exposure).test_client()
            issued = client.post(
                "/v1/recovery-sessions",
                headers={
                    "Host": "setup.example",
                    "Authorization": f"Bearer {'n' * 43}",
                    "Idempotency-Key": "https-recovery",
                },
                json={},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            token = parse_qs(
                urlsplit(issued.get_json()["url"]).fragment
            )["token"][0]
            exchanged = client.post(
                "/v1/recovery/exchange",
                headers=forwarded,
                json={"token": token},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )

        self.assertEqual(200, issued.status_code)
        self.assertEqual(200, exchanged.status_code)
        self.assertIn("Secure", exchanged.headers["Set-Cookie"])
        self.assertIn("Max-Age=", exchanged.headers["Set-Cookie"])
        self.assertEqual(
            "max-age=31536000",
            exchanged.headers["Strict-Transport-Security"],
        )

    def test_private_http_browser_can_choose_session_only_or_remembered_cookie(self):
        exposure = EndpointExposure(
            mode=AccessMode.PRIVATE_NETWORK,
            bind_host="0.0.0.0",
            port=8098,
            public_url="http://gpu-box.local:8098",
            allow_insecure_remote=True,
        )
        headers = {
            "Host": "gpu-box.local:8098",
            "Origin": "http://gpu-box.local:8098",
        }
        remote = {"REMOTE_ADDR": "192.0.2.44"}
        with tempfile.TemporaryDirectory() as directory:
            client = self._api(directory, exposure).test_client()

            def exchange(key, remember):
                issued = client.post(
                    "/v1/recovery-sessions",
                    headers={
                        "Host": "gpu-box.local:8098",
                        "Authorization": f"Bearer {'n' * 43}",
                        "Idempotency-Key": key,
                    },
                    json={},
                    environ_overrides={
                        "REMOTE_ADDR": "127.0.0.1"
                    },
                )
                token = parse_qs(
                    urlsplit(issued.get_json()["url"]).fragment
                )["token"][0]
                return client.post(
                    "/v1/recovery/exchange",
                    headers=headers,
                    json={"token": token, "remember": remember},
                    environ_overrides=remote,
                )

            transient = exchange("private-transient", False)
            remembered = exchange("private-remembered", True)

        self.assertEqual(200, transient.status_code)
        self.assertFalse(transient.get_json()["session"]["remembered"])
        self.assertNotIn("Max-Age=", transient.headers["Set-Cookie"])
        self.assertNotIn("Secure", transient.headers["Set-Cookie"])
        self.assertEqual(200, remembered.status_code)
        self.assertTrue(remembered.get_json()["session"]["remembered"])
        self.assertIn("Max-Age=", remembered.headers["Set-Cookie"])
        self.assertTrue(
            remembered.get_json()["policy"]["insecure_private_http"]
        )
        self.assertEqual(
            7 * 24 * 60 * 60,
            remembered.get_json()["policy"]["remembered_idle_ttl_seconds"],
        )

    def test_local_application_profile_can_be_saved_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._api(
                directory,
                EndpointExposure(port=0),
            ).test_client()
            response = client.put(
                "/v1/network/application",
                headers={
                    "Authorization": f"Bearer {'n' * 43}",
                    "Idempotency-Key": "local-before-install",
                },
                json={
                    "exposure": {
                        "mode": "local",
                        "bind_host": "127.0.0.1",
                        "port": 18197,
                        "public_url": None,
                        "trusted_hosts": [],
                        "proxy_hops": 0,
                        "allow_insecure_remote": False,
                    }
                },
            )
            persisted = load_network_configuration(
                WorkspaceLayout.from_value(directory),
                environment={},
            )

        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual(18197, persisted.application.port)


class PandratorServeNetworkTests(unittest.TestCase):
    @staticmethod
    def _args(**overrides):
        values = {
            "host": "127.0.0.1",
            "port": 8097,
            "threads": 6,
            "trusted_host": [],
            "proxy_hops": 0,
            "public_url": None,
            "allow_insecure_remote": False,
            "open_browser": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_profiles_require_exact_remote_url_and_matching_private_port(self):
        with self.assertRaisesRegex(ValueError, "public-url"):
            serve_network_profile(
                self._args(
                    host="0.0.0.0",
                    allow_insecure_remote=True,
                )
            )
        with self.assertRaisesRegex(ValueError, "must match"):
            serve_network_profile(
                self._args(
                    host="0.0.0.0",
                    public_url="http://gpu-box.local:8098",
                    allow_insecure_remote=True,
                )
            )
        with self.assertRaisesRegex(ValueError, "exact"):
            serve_network_profile(
                self._args(
                    host="0.0.0.0",
                    public_url="https://pandrator.example",
                    proxy_hops=1,
                    trusted_host=["*.example"],
                )
            )

    def test_https_profile_bootstraps_owner_without_leaking_secret(self):
        args = self._args(
            host="127.0.0.1",
            public_url="https://pandrator.example",
            proxy_hops=1,
        )
        paths = SimpleNamespace(root=Path("data-root"))
        database = mock.Mock()
        authentication = mock.Mock()
        authentication.initialized.return_value = False
        application = object()
        with (
            mock.patch.dict(
                os.environ,
                {"PANDRATOR_OWNER_PASSWORD": "remote-owner-password"},
                clear=False,
            ),
            mock.patch(
                "pandrator.web.cli._database",
                return_value=(paths, database),
            ),
            mock.patch(
                "pandrator.web.cli.AuthService",
                return_value=authentication,
            ),
            mock.patch(
                "pandrator.web.cli.create_app",
                return_value=application,
            ) as create_app,
            mock.patch("pandrator.web.cli.waitress_serve") as serve,
        ):
            result = command_serve(args)
            password_still_present = "PANDRATOR_OWNER_PASSWORD" in os.environ

        self.assertEqual(0, result)
        self.assertFalse(password_still_present)
        authentication.initialize_owner.assert_called_once_with(
            "remote-owner-password"
        )
        database.dispose.assert_called_once_with()
        self.assertTrue(create_app.call_args.kwargs["secure_cookies"])
        self.assertIn(
            "pandrator.example",
            create_app.call_args.kwargs["trusted_hosts"],
        )
        serve.assert_called_once()


class RecoveryNetworkAssetTests(unittest.TestCase):
    def test_recovery_bundle_exposes_guided_network_controls_without_inline_style(self):
        root = (
            Path(__file__).resolve().parents[1]
            / "pandrator_manager"
            / "recovery_ui"
            / "static"
        )
        html = (root / "index.html").read_text(encoding="utf-8")
        script = (root / "app.js").read_text(encoding="utf-8")
        self.assertIn("Use Pandrator from another device", html)
        self.assertIn("private_network", html)
        self.assertIn("/v1/network/application", script)
        self.assertNotIn(".style.gridColumn", script)
        self.assertNotIn(".style.width", script)


if __name__ == "__main__":
    unittest.main()
