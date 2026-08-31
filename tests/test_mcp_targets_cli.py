import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from pandrator_mcp.__main__ import main
from pandrator_mcp.credentials import (
    CredentialReference,
    CredentialResolver,
    SecretValue,
)
from pandrator_mcp.errors import TargetResolutionError
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.targets import (
    LocalSourceRoot,
    TargetIdentityExpectation,
    TargetProfile,
    TargetStore,
)


class MemoryCredentialBackend:
    name = "keyring"

    def __init__(self):
        self.values = {}

    def resolve(self, reference):
        return SecretValue(self.values[reference.reference])

    def store(self, reference, value):
        self.values[reference.reference] = value.reveal()

    def delete(self, reference):
        self.values.pop(reference.reference, None)


class TargetStoreTests(unittest.TestCase):
    def test_local_roots_round_trip_without_entering_tool_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            store = TargetStore(base / "targets.json")
            store.put(
                TargetProfile(
                    name="local",
                    mode=TargetMode.LOCAL_MANAGED,
                    workspace=str(base),
                )
            )
            updated = store.configure_local_paths(
                "local",
                source_roots=(
                    LocalSourceRoot(name="downloads", path=str(base / "inputs")),
                ),
                output_root=str(base / "outputs"),
            )
            self.assertEqual("downloads", updated.local_source_roots[0].name)
            loaded = store.load(missing_ok=False)[0]
            self.assertEqual(str(base / "inputs"), loaded.local_source_roots[0].path)
            self.assertEqual(str(base / "outputs"), loaded.local_output_root)

    def test_atomic_store_round_trip_update_and_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "targets.json"
            store = TargetStore(path)
            second = TargetProfile(
                name="zeta",
                mode=TargetMode.LOCAL_MANAGED,
                workspace="C:/Pandrator",
            )
            first = TargetProfile(
                name="alpha",
                mode=TargetMode.EXTERNAL_HTTPS,
                application_origin="https://pandrator.example",
            )
            store.put(second)
            store.put(first)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                ["alpha", "zeta"],
                [item["name"] for item in payload["targets"]],
            )
            self.assertNotIn("token", path.read_text(encoding="utf-8").lower())

            updated = store.update_identity(
                "alpha",
                TargetIdentityExpectation(
                    application_instance_id="application-id",
                    canonical_application_origin="https://pandrator.example",
                ),
            )
            self.assertEqual(
                "application-id",
                updated.expected_identity.application_instance_id,
            )
            removed = store.remove("zeta")
            self.assertEqual("zeta", removed.name)
            self.assertEqual(["alpha"], [item.name for item in store.load()])

    def test_duplicate_and_missing_target_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TargetStore(Path(directory) / "targets.json")
            profile = TargetProfile(
                name="local",
                mode=TargetMode.LOCAL_MANAGED,
                workspace="C:/Pandrator",
            )
            store.put(profile)
            with self.assertRaises(ValueError):
                store.put(profile)
            with self.assertRaises(TargetResolutionError):
                store.remove("missing")


class TargetCliTests(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(list(arguments))
        return result, stdout.getvalue(), stderr.getvalue()

    def test_add_list_and_confirmed_remove_never_emit_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "targets.json")
            result, output, error = self.invoke(
                "target",
                "--config",
                path,
                "add",
                "remote",
                "--mode",
                "external",
                "--origin",
                "https://pandrator.example",
                "--credential-backend",
                "environment",
                "--credential-reference",
                "PANDRATOR_REMOTE_TOKEN",
            )
            self.assertEqual(0, result, error)
            self.assertNotIn("PANDRATOR_REMOTE_TOKEN", output)

            result, output, error = self.invoke(
                "target",
                "--config",
                path,
                "list",
            )
            self.assertEqual(0, result, error)
            self.assertIn('"application_credential_configured": true', output)
            self.assertNotIn("PANDRATOR_REMOTE_TOKEN", output)

            result, _output, error = self.invoke(
                "target",
                "--config",
                path,
                "remove",
                "remote",
            )
            self.assertEqual(2, result)
            self.assertIn("--yes", error)

            result, output, error = self.invoke(
                "target",
                "--config",
                path,
                "remove",
                "remote",
                "--yes",
            )
            self.assertEqual(2, result)
            self.assertIn("--delete-local-credentials", error)

            result, output, error = self.invoke(
                "target",
                "--config",
                path,
                "remove",
                "remote",
                "--yes",
                "--keep-local-credentials",
            )
            self.assertEqual(0, result, error)
            self.assertIn('"removed": "remote"', output)
            self.assertIn(
                '"local_credentials_preserved": true',
                output,
            )
            self.assertIn('"remote_clients_revoked": false', output)

    def test_local_source_and_output_roots_are_managed_explicitly(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            config = str(base / "targets.json")
            result, _output, error = self.invoke(
                "target",
                "--config",
                config,
                "add",
                "local",
                "--mode",
                "local",
                "--workspace",
                str(base),
            )
            self.assertEqual(0, result, error)
            result, output, error = self.invoke(
                "target",
                "--config",
                config,
                "source-root-add",
                "local",
                "downloads",
                str(base / "Downloads"),
            )
            self.assertEqual(0, result, error)
            self.assertIn('"name": "downloads"', output)
            result, output, error = self.invoke(
                "target",
                "--config",
                config,
                "output-root-set",
                "local",
                str(base / "outputs"),
            )
            self.assertEqual(0, result, error)
            self.assertIn(str(base / "outputs"), output)
            result, output, error = self.invoke(
                "target",
                "--config",
                config,
                "source-root-list",
                "local",
            )
            self.assertEqual(0, result, error)
            self.assertIn("downloads", output)

    def test_logout_deletes_keyring_secret_and_retains_remote_guidance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            client_id = "b344c4fd-7285-44f0-922e-912475701c0c"
            reference = CredentialReference(
                backend="keyring",
                reference=f"target:{client_id}:application",
                audience="application",
            )
            TargetStore(path).put(
                TargetProfile(
                    name="remote",
                    mode=TargetMode.EXTERNAL_HTTPS,
                    application_origin="https://pandrator.example",
                    automation_client_id=client_id,
                    application_credential=reference,
                    enrolled_subject=f"automation:{client_id}",
                    credential_expires_at="2026-08-01T00:00:00Z",
                )
            )
            backend = MemoryCredentialBackend()
            backend.values[reference.reference] = "not-a-real-token"
            resolver = CredentialResolver((backend,))

            with patch(
                "pandrator_mcp.__main__.CredentialResolver",
                return_value=resolver,
            ):
                result, output, error = self.invoke(
                    "target",
                    "--config",
                    str(path),
                    "logout",
                    "remote",
                    "--yes",
                )

            self.assertEqual(0, result, error)
            self.assertEqual({}, backend.values)
            updated = TargetStore(path).load(missing_ok=False)[0]
            self.assertIsNone(updated.application_credential)
            self.assertIsNone(updated.enrolled_subject)
            self.assertIsNone(updated.credential_expires_at)
            self.assertEqual(
                client_id,
                updated.automation_client_id,
            )
            self.assertIn('"local_credential_deleted": true', output)
            self.assertIn('"remote_client_revoked": false', output)
            self.assertIn(client_id, output)
            self.assertNotIn(reference.reference, output)

    def test_configure_recovery_preserves_application_target_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "targets.json")
            result, _output, error = self.invoke(
                "target",
                "--config",
                path,
                "add",
                "remote",
                "--mode",
                "external",
                "--origin",
                "https://pandrator.example",
                "--scope",
                "app.read",
            )
            self.assertEqual(0, result, error)
            before = TargetStore(path).load()[0]

            result, output, error = self.invoke(
                "target",
                "--config",
                path,
                "configure-recovery",
                "remote",
                "--origin",
                "https://recovery.pandrator.example",
                "--recovery-scope",
                "manager.read",
                "--recovery-scope",
                "manager.runtime",
            )

            self.assertEqual(0, result, error)
            self.assertNotIn(
                str(before.automation_client_id),
                output,
            )
            after = TargetStore(path).load()[0]
            self.assertEqual(
                before.application_origin,
                after.application_origin,
            )
            self.assertEqual(
                before.automation_client_id,
                after.automation_client_id,
            )
            self.assertEqual(
                before.automation_client_id,
                after.manager_automation_client_id,
            )
            self.assertEqual(
                "https://recovery.pandrator.example",
                after.manager_recovery_origin,
            )
            self.assertEqual(
                ("manager.read", "manager.runtime"),
                after.manager_requested_scopes,
            )

    def test_configure_recovery_refuses_to_replace_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.json"
            store = TargetStore(path)
            store.put(
                TargetProfile(
                    name="remote",
                    mode=TargetMode.EXTERNAL_HTTPS,
                    application_origin="https://pandrator.example",
                    manager_recovery_origin=("https://recovery.pandrator.example"),
                    manager_recovery_credential=CredentialReference(
                        backend="keyring",
                        reference="remote/recovery",
                        audience="manager_recovery",
                    ),
                )
            )

            result, _output, error = self.invoke(
                "target",
                "--config",
                str(path),
                "configure-recovery",
                "remote",
                "--origin",
                "https://new-recovery.pandrator.example",
            )

            self.assertEqual(2, result)
            self.assertIn("Revoke it before changing", error)


if __name__ == "__main__":
    unittest.main()
