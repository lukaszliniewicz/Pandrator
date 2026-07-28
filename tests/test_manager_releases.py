import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import pandrator_manager.releases.handoff as handoff_module
import pandrator_manager.releases.trust as trust_module
from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.auth import ensure_client_secret
from pandrator_manager.errors import ConflictError, ManagerError
from pandrator_manager.models import HealthResult, HealthState, OperationState
from pandrator_manager.operations import OperationEngine
from pandrator_manager.releases import (
    ReleaseActivationError,
    ReleaseSlotManager,
    ReleaseTrustNotProvisioned,
    TrustStore,
    canonical_json,
    release_cache_path,
)
from pandrator_manager.releases.handoff import (
    pending_handoffs,
    read_handoff,
)
from pandrator_manager.releases.models import ReleaseArtifact
from pandrator_manager.runtime_specs import pandrator_runtime_specs


def _public(private: Ed25519PrivateKey) -> str:
    return base64.b64encode(
        private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()


def _signed(
    private: Ed25519PrivateKey,
    *,
    key_id: str = "test",
    version: str = "1.0.0",
    sequence: int = 1,
    product: str = "pandrator",
    rotation: dict | None = None,
    artifact: dict | None = None,
) -> dict:
    payload = {
        "schema_version": 1,
        "product": product,
        "channel": "stable",
        "version": version,
        "sequence": sequence,
        "published_at": datetime.now(timezone.utc).isoformat(),
        "minimum_manager_version": "0.1",
        "artifacts": [
            artifact
            or {
                "filename": "pandrator.whl",
                "url": "https://releases.example.invalid/pandrator.whl",
                "sha256": "a" * 64,
                "size_bytes": 123,
                "kind": "wheel",
                "systems": ["Windows"],
                "architectures": ["AMD64"],
                "python_tags": ["cp312"],
            }
        ],
        "key_rotation": rotation,
    }
    signature = private.sign(canonical_json(payload))
    return {
        "signed": payload,
        "signatures": [
            {
                "key_id": key_id,
                "signature": base64.b64encode(signature).decode(),
            }
        ],
    }


def _release_bundle(
    path: Path,
    *,
    version: str = "1.0.0",
    product: str = "pandrator",
) -> tuple[str, int]:
    python_name = "runtime/python.exe" if os.name == "nt" else "runtime/bin/python"
    executable = zipfile.ZipInfo(python_name)
    executable.external_attr = (0o100755 << 16)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "pandrator-release.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "product": product,
                    "version": version,
                    "application_root": "app",
                    "python": python_name,
                }
            ),
        )
        archive.writestr("app/pandrator/__init__.py", "")
        archive.writestr(executable, b"private runtime")
    payload = path.read_bytes()
    return hashlib.sha256(payload).hexdigest(), len(payload)


class _ReleaseSupervisor:
    def __init__(
        self,
        specs,
        database: Path,
        *,
        fail_new_health: bool = False,
    ):
        self.specs = {spec.service_id: spec for spec in specs}
        self.running = set(self.specs)
        self.desired = set(self.specs)
        self.database = database
        self.fail_new_health = fail_new_health

    def snapshot(self):
        return [
            SimpleNamespace(
                id=service_id,
                process=(object() if service_id in self.running else None),
                desired_running=service_id in self.desired,
            )
            for service_id in self.specs
        ]

    def spec(self, service_id):
        selected = self.specs.get(service_id)
        return selected.model_copy(deep=True) if selected is not None else None

    def stop(self, service_id):
        self.running.discard(service_id)
        self.desired.discard(service_id)
        return SimpleNamespace(id=service_id)

    def start(self, service_id):
        spec = self.specs[service_id]
        expected = spec.readiness.expected_json.get("version")
        if (
            service_id == "pandrator.api"
            and expected
            and self.fail_new_health
        ):
            with closing(sqlite3.connect(self.database)) as connection:
                with connection:
                    connection.execute("UPDATE state SET value='migrated'")
            raise RuntimeError("new application health failed")
        self.running.add(service_id)
        self.desired.add(service_id)
        return SimpleNamespace(
            health=HealthResult(
                state=HealthState.HEALTHY,
                service_id=service_id,
                details={"version": expected} if expected else {},
            )
        )

    def replace_spec(self, spec):
        if spec.service_id in self.running:
            raise RuntimeError("cannot replace a running service")
        previous = self.specs.get(spec.service_id)
        self.specs[spec.service_id] = spec
        return previous

    def unregister(self, service_id):
        if service_id in self.running:
            raise RuntimeError("cannot unregister a running service")
        return self.specs.pop(service_id, None)

    def register(self, spec):
        if spec.service_id in self.specs:
            raise ValueError("duplicate service")
        self.specs[spec.service_id] = spec


class _FakeLaunchedProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.terminated = True
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class ReleaseTrustTests(unittest.TestCase):
    def setUp(self):
        self.private = Ed25519PrivateKey.generate()
        self.trust = TrustStore({"test": _public(self.private)})

    def test_signature_target_selection_and_exact_replay(self):
        document = _signed(self.private)
        verified = self.trust.verify(document)
        artifact = verified.select_artifact(
            system="windows",
            architecture="amd64",
            python_tag="cp312",
        )
        self.assertEqual(artifact.filename, "pandrator.whl")
        replay = self.trust.verify(
            document,
            current_version="1.0.0",
            last_sequence=1,
            current_manifest_digest=verified.digest,
        )
        self.assertEqual(replay.digest, verified.digest)

    def test_tampering_downgrade_and_sequence_reuse_are_rejected(self):
        document = _signed(self.private)
        document["signed"]["version"] = "1.0.1"
        with self.assertRaisesRegex(ValueError, "threshold"):
            self.trust.verify(document)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            self.trust.verify(
                _signed(self.private, version="0.9.0", sequence=2),
                current_version="1.0.0",
                last_sequence=1,
            )
        with self.assertRaisesRegex(ValueError, "sequence"):
            self.trust.verify(
                _signed(self.private, version="1.1.0", sequence=1),
                current_version="1.0.0",
                last_sequence=1,
            )

    def test_signed_key_rotation_has_a_future_activation_boundary(self):
        next_private = Ed25519PrivateKey.generate()
        rotation = {
            "activates_at_sequence": 2,
            "threshold": 1,
            "keys": [
                {
                    "key_id": "next",
                    "algorithm": "ed25519",
                    "public_key": _public(next_private),
                }
            ],
        }
        authorization = self.trust.verify(
            _signed(self.private, rotation=rotation)
        )
        rotated = self.trust.rotated(authorization)
        with self.assertRaisesRegex(ValueError, "not active"):
            rotated.verify(
                _signed(
                    next_private,
                    key_id="next",
                    version="1.0.1",
                    sequence=1,
                )
            )
        verified = rotated.verify(
            _signed(
                next_private,
                key_id="next",
                version="1.1.0",
                sequence=2,
            )
        )
        self.assertEqual(verified.verified_key_ids, ("next",))

    def test_unprovisioned_embedded_trust_fails_closed(self):
        with mock.patch.object(
            trust_module,
            "EMBEDDED_RELEASE_KEYS",
            {},
        ), self.assertRaisesRegex(
            ReleaseTrustNotProvisioned,
            "trust is not provisioned",
        ):
            TrustStore.embedded()


class ManagerUpdateDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private = Ed25519PrivateKey.generate()
        self.application = create_application(
            self.temporary.name,
            release_trust_root=TrustStore(
                {"test": _public(self.private)}
            ),
        )

    def _manifest(self, version="99.0.0"):
        return _signed(
            self.private,
            version=version,
            product="pandrator-manager",
            artifact={
                "filename": "pandrator-manager-host.zip",
                "url": (
                    "https://releases.example.invalid/"
                    "pandrator-manager-host.zip"
                ),
                "sha256": "a" * 64,
                "size_bytes": 123,
                "kind": "zip",
                "systems": [self.application.context.system],
                "architectures": [self.application.context.architecture],
                "python_tags": [],
            },
        )

    def test_discovery_verifies_signature_before_offering_update(self):
        manifest = self._manifest()
        with mock.patch(
            "pandrator_manager.application.fetch_manager_manifest",
            return_value=manifest,
        ):
            update = self.application.manager_update()

        self.assertEqual("available", update["status"])
        self.assertEqual("99.0.0", update["version"])
        self.assertEqual(manifest, update["manifest"])

        tampered = json.loads(json.dumps(manifest))
        tampered["signed"]["version"] = "100.0.0"
        with mock.patch(
            "pandrator_manager.application.fetch_manager_manifest",
            return_value=tampered,
        ), self.assertRaises(ManagerError) as raised:
            self.application.manager_update()
        self.assertEqual("release_verification_failed", raised.exception.code)

    def test_authenticated_update_endpoint_returns_verified_manifest(self):
        secret = "u" * 43
        self.application.instance_id = "update-discovery-api"
        client = create_api(
            self.application,
            mock.Mock(),
            client_secret=secret,
        ).test_client()
        with mock.patch(
            "pandrator_manager.application.fetch_manager_manifest",
            return_value=self._manifest(),
        ):
            self.assertEqual(
                401,
                client.get("/v1/releases/manager-update").status_code,
            )
            response = client.get(
                "/v1/releases/manager-update",
                headers={"Authorization": f"Bearer {secret}"},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("available", response.get_json()["status"])


class ReleaseSlotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.application = create_application(self.temporary.name)
        private = Ed25519PrivateKey.generate()
        self.manifest = TrustStore({"test": _public(private)}).verify(
            _signed(private)
        )

    def _database(self) -> Path:
        path = self.application.context.layout.data / "app.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.execute("CREATE TABLE state(value TEXT)")
                connection.execute("INSERT INTO state(value) VALUES ('old')")
        return path

    @staticmethod
    def _value(database: Path) -> str:
        with closing(sqlite3.connect(database)) as connection:
            return connection.execute("SELECT value FROM state").fetchone()[0]

    def test_failed_health_restores_pointer_and_database(self):
        layout = self.application.context.layout
        old = layout.app_versions / "0.9.0"
        old.mkdir(parents=True)
        pointer = layout.root / "app" / "current.json"
        pointer.write_text(
            json.dumps({"version": "0.9.0", "path": str(old)}),
            encoding="utf-8",
        )
        staged = layout.staging / "release" / "app"
        staged.mkdir(parents=True)
        (staged / "marker").write_text("new", encoding="utf-8")
        database = self._database()

        def migrate(_slot):
            with closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute("UPDATE state SET value='new'")

        with self.assertRaises(ReleaseActivationError):
            ReleaseSlotManager(layout, self.application.store).activate(
                self.manifest,
                staged,
                migrate=migrate,
                health_check=lambda _slot: (_ for _ in ()).throw(
                    RuntimeError("unhealthy")
                ),
                database=database,
            )

        restored = json.loads(pointer.read_text(encoding="utf-8"))
        self.assertEqual(restored["version"], "0.9.0")
        self.assertEqual(self._value(database), "old")
        slots = self.application.store.release_slots("pandrator")
        self.assertFalse(slots[0]["active"])
        self.assertFalse(slots[0]["healthy"])

    def test_healthy_release_becomes_the_only_active_slot(self):
        layout = self.application.context.layout
        staged = layout.staging / "release" / "app"
        staged.mkdir(parents=True)
        (staged / "marker").write_text("new", encoding="utf-8")
        selected = ReleaseSlotManager(
            layout,
            self.application.store,
        ).activate(
            self.manifest,
            staged,
            health_check=lambda slot: self.assertTrue(
                (slot / "marker").is_file()
            ),
        )

        self.assertTrue(selected.is_dir())
        active = [
            slot
            for slot in self.application.store.release_slots("pandrator")
            if slot["active"]
        ]
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["healthy"])
        self.assertEqual(
            hashlib.sha256(
                canonical_json(self.manifest.raw_signed)
            ).hexdigest(),
            self.manifest.digest,
        )


class DurableApplicationReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private = Ed25519PrivateKey.generate()
        self.trust = TrustStore({"test": _public(self.private)})
        self.application = create_application(
            self.temporary.name,
            release_trust_root=self.trust,
        )
        self.layout = self.application.context.layout
        self.old_slot = self.layout.app_versions / "0.9.0"
        self.old_slot.mkdir(parents=True)
        (self.old_slot / "old.txt").write_text("old", encoding="utf-8")
        self.pointer = self.layout.root / "app" / "current.json"
        self.pointer.write_text(
            json.dumps(
                {
                    "product": "pandrator",
                    "version": "0.9.0",
                    "path": str(self.old_slot),
                }
            ),
            encoding="utf-8",
        )
        self.database = self.layout.data / "pandrator.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            with connection:
                connection.execute("CREATE TABLE state(value TEXT)")
                connection.execute("INSERT INTO state(value) VALUES ('old')")

    def _plan(self):
        manifest = self._prepare_manifest()
        return self.application.release_plan(
            manifest,
            offline=True,
            start_after_activation=True,
        )

    def _prepare_manifest(
        self,
        *,
        private=None,
        key_id="test",
        version="1.0.0",
        sequence=1,
        rotation=None,
    ):
        signing_key = private or self.private
        fixture = self.layout.cache / f"release-fixture-{version}.zip"
        digest, size = _release_bundle(fixture, version=version)
        artifact = {
            "filename": f"pandrator-{version}-host.zip",
            "url": "https://releases.example.invalid/pandrator.zip",
            "sha256": digest,
            "size_bytes": size,
            "kind": "zip",
            "systems": [self.application.context.system],
            "architectures": [self.application.context.architecture],
            "python_tags": [],
        }
        manifest = _signed(
            signing_key,
            key_id=key_id,
            version=version,
            sequence=sequence,
            rotation=rotation,
            artifact=artifact,
        )
        selected_artifact = ReleaseArtifact.model_validate(artifact)
        cached = release_cache_path(self.layout, selected_artifact)
        cached.parent.mkdir(parents=True, exist_ok=True)
        os.replace(fixture, cached)
        return manifest

    def _execute(self, *, fail_new_health: bool):
        return self._execute_manifest(
            self._prepare_manifest(),
            fail_new_health=fail_new_health,
            idempotency_key=f"release-{fail_new_health}",
        )

    def _execute_manifest(
        self,
        manifest,
        *,
        fail_new_health: bool,
        idempotency_key: str,
    ):
        old_specs = pandrator_runtime_specs(self.layout)
        supervisor = _ReleaseSupervisor(
            old_specs,
            self.database,
            fail_new_health=fail_new_health,
        )
        plan = self.application.release_plan(
            manifest,
            offline=True,
            start_after_activation=True,
        )
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=idempotency_key,
        )
        self.assertTrue(created)
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            supervisor=supervisor,
            release_authority=self.application.release_authority,
        )
        engine._execute(operation.id)
        return (
            plan,
            self.application.store.get_operation(operation.id),
            supervisor,
        )

    @staticmethod
    def _database_value(path: Path) -> str:
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute(
                "SELECT value FROM state"
            ).fetchone()[0]

    def test_success_atomically_accepts_release_slot_and_ownership(self):
        plan, operation, supervisor = self._execute(
            fail_new_health=False
        )
        self.assertEqual(operation.state, OperationState.SUCCEEDED)
        release = self.application.store.accepted_release("pandrator")
        self.assertIsNotNone(release)
        self.assertEqual(release["version"], "1.0.0")
        self.assertEqual(release["sequence"], 1)
        self.assertEqual(
            release["manifest_digest"],
            plan.impacts["release"]["manifest_digest"],
        )
        pointer = json.loads(self.pointer.read_text(encoding="utf-8"))
        self.assertEqual(pointer["version"], "1.0.0")
        active = [
            slot
            for slot in self.application.store.release_slots("pandrator")
            if slot["active"]
        ]
        self.assertEqual(len(active), 1)
        self.assertTrue(active[0]["healthy"])
        ownership = self.application.store.owned_paths()
        self.assertTrue(
            any(
                item["owner_kind"] == "release"
                and item["owner_id"] == "pandrator"
                for item in ownership
            )
        )
        self.assertEqual(
            supervisor.specs[
                "pandrator.api"
            ].readiness.expected_json["version"],
            "1.0.0",
        )

    def test_application_release_reconciles_legacy_data_after_stopping_services(self):
        legacy_output = (
            self.layout.root
            / "Pandrator"
            / "Outputs"
            / "Legacy Book"
        )
        legacy_output.mkdir(parents=True)
        (legacy_output / "chapter.wav").write_bytes(b"legacy audio")

        plan, operation, _supervisor = self._execute(
            fail_new_health=False
        )

        self.assertEqual(operation.state, OperationState.SUCCEEDED)
        task_ids = [task.id for task in plan.tasks]
        self.assertLess(
            task_ids.index("release:stop-application"),
            task_ids.index("release:legacy-data"),
        )
        self.assertLess(
            task_ids.index("release:legacy-data"),
            task_ids.index("release:activate"),
        )
        self.assertEqual(
            (
                self.layout.data
                / "Outputs"
                / "Legacy Book"
                / "chapter.wav"
            ).read_bytes(),
            b"legacy audio",
        )
        self.assertTrue((legacy_output / "chapter.wav").is_file())

    def test_failed_new_health_restores_pointer_database_specs_and_services(self):
        _plan, operation, supervisor = self._execute(
            fail_new_health=True
        )
        self.assertEqual(operation.state, OperationState.FAILED)
        pointer = json.loads(self.pointer.read_text(encoding="utf-8"))
        self.assertEqual(pointer["version"], "0.9.0")
        self.assertEqual(self._database_value(self.database), "old")
        self.assertFalse((self.layout.app_versions / "1.0.0").exists())
        self.assertIsNone(
            self.application.store.accepted_release("pandrator")
        )
        self.assertEqual(
            supervisor.specs[
                "pandrator.api"
            ].readiness.expected_json,
            {},
        )
        self.assertEqual(
            supervisor.running,
            {"pandrator.api", "pandrator.worker"},
        )

    def test_exact_signed_replay_is_a_no_mutation_plan(self):
        plan, operation, _supervisor = self._execute(
            fail_new_health=False
        )
        self.assertEqual(operation.state, OperationState.SUCCEEDED)
        envelope = self.application.store.accepted_release(
            "pandrator"
        )["envelope"]
        replay = self.application.release_plan(
            envelope,
            expected_revision=self.application.store.configuration_revision(),
            persist=False,
        )
        self.assertEqual(replay.tasks, ())
        self.assertTrue(replay.impacts["release"]["exact_replay"])
        self.assertEqual(
            replay.impacts["release"]["manifest_digest"],
            plan.impacts["release"]["manifest_digest"],
        )

    def test_persisted_signed_rotation_authorizes_the_next_release(self):
        next_private = Ed25519PrivateKey.generate()
        rotation = {
            "activates_at_sequence": 2,
            "threshold": 1,
            "keys": [
                {
                    "key_id": "next",
                    "algorithm": "ed25519",
                    "public_key": _public(next_private),
                }
            ],
        }
        first_manifest = self._prepare_manifest(rotation=rotation)
        _first_plan, first, _ = self._execute_manifest(
            first_manifest,
            fail_new_health=False,
            idempotency_key="rotation-one",
        )
        self.assertEqual(first.state, OperationState.SUCCEEDED)

        second_manifest = self._prepare_manifest(
            private=next_private,
            key_id="next",
            version="1.1.0",
            sequence=2,
        )
        second_plan, second, _ = self._execute_manifest(
            second_manifest,
            fail_new_health=False,
            idempotency_key="rotation-two",
        )
        self.assertEqual(second.state, OperationState.SUCCEEDED)
        accepted = self.application.store.accepted_releases("pandrator")
        self.assertEqual(
            [(item["sequence"], item["version"]) for item in accepted],
            [(1, "1.0.0"), (2, "1.1.0")],
        )
        self.assertEqual(accepted[-1]["verified_key_ids"], ("next",))
        self.assertEqual(
            accepted[-1]["manifest_digest"],
            second_plan.impacts["release"]["manifest_digest"],
        )

    def test_release_plan_api_is_authenticated_typed_and_idempotent(self):
        manifest = self._prepare_manifest()
        supervisor = _ReleaseSupervisor(
            pandrator_runtime_specs(self.layout),
            self.database,
        )
        secret = "r" * 43
        self.application.instance_id = "release-api"
        client = create_api(
            self.application,
            supervisor,
            client_secret=secret,
        ).test_client()
        body = {
            "manifest": manifest,
            "offline": True,
            "start_after_activation": True,
        }
        self.assertEqual(
            client.post("/v1/releases/plans", json=body).status_code,
            401,
        )
        headers = {
            "Authorization": f"Bearer {secret}",
            "Idempotency-Key": "release-plan",
        }
        first = client.post(
            "/v1/releases/plans",
            headers=headers,
            json=body,
        )
        repeated = client.post(
            "/v1/releases/plans",
            headers=headers,
            json=body,
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(
            first.get_json()["id"],
            repeated.get_json()["id"],
        )
        self.assertEqual(
            first.get_json()["impacts"]["release"]["version"],
            "1.0.0",
        )
        listed = client.get("/v1/releases", headers=headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.get_json()["current"], {})

    def test_pypi_managed_manager_update_returns_external_commands(self):
        fixture = self.layout.cache / "manager-release.zip"
        digest, size = _release_bundle(
            fixture,
            version="1.0.0",
            product="pandrator-manager",
        )
        manifest = _signed(
            self.private,
            product="pandrator-manager",
            artifact={
                "filename": "pandrator-manager.zip",
                "url": "https://releases.example.invalid/manager.zip",
                "sha256": digest,
                "size_bytes": size,
                "kind": "zip",
                "systems": [self.application.context.system],
                "architectures": [self.application.context.architecture],
                "python_tags": [],
            },
        )
        with self.assertRaisesRegex(
            ManagerError,
            "Python tool installer",
        ) as raised:
            self.application.release_plan(manifest, persist=False)
        error = raised.exception
        self.assertEqual(
            getattr(error, "code", None),
            "external_manager_update_required",
        )
        self.assertIn(
            "pipx upgrade pandrator-manager",
            error.details["commands"],
        )

    def _prepare_native_manager_plan(self):
        ensure_client_secret(self.layout.credential)
        fixture = self.layout.cache / "native-manager-release.zip"
        digest, size = _release_bundle(
            fixture,
            version="1.0.0",
            product="pandrator-manager",
        )
        artifact = {
            "filename": "pandrator-manager-native.zip",
            "url": "https://releases.example.invalid/manager-native.zip",
            "sha256": digest,
            "size_bytes": size,
            "kind": "zip",
            "systems": [self.application.context.system],
            "architectures": [self.application.context.architecture],
            "python_tags": [],
        }
        manifest = _signed(
            self.private,
            product="pandrator-manager",
            artifact=artifact,
        )
        selected = self.trust.verify(manifest).select_artifact(
            system=self.application.context.system,
            architecture=self.application.context.architecture,
        )
        cached = release_cache_path(self.layout, selected)
        cached.parent.mkdir(parents=True, exist_ok=True)
        os.replace(fixture, cached)
        self.application.release_planner._manager_is_native = lambda: True
        return self.application.release_plan(
            manifest,
            offline=True,
        )

    def _submit_manager_plan(self, plan):
        operation, _created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=f"manager-{plan.id}",
        )
        return operation

    def _prepare_pending_manager_handoff(self):
        plan = self._prepare_native_manager_plan()
        operation = self._submit_manager_plan(plan)
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            release_authority=self.application.release_authority,
            manager_handoff_callback=lambda _execution, _result: None,
        )
        engine._execute(operation.id)
        self.assertEqual(
            self.application.store.get_operation(operation.id).state,
            OperationState.HANDOFF_PENDING,
        )
        return plan, operation

    def test_native_manager_update_pauses_for_authenticated_external_handoff(self):
        plan = self._prepare_native_manager_plan()
        operation = self._submit_manager_plan(plan)
        callbacks = []
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            release_authority=self.application.release_authority,
            manager_handoff_callback=(
                lambda execution, result: callbacks.append(
                    (execution.operation.id, result)
                )
            ),
        )
        engine._execute(operation.id)
        pending = self.application.store.get_operation(operation.id)
        self.assertEqual(
            pending.state,
            OperationState.HANDOFF_PENDING,
        )
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(pending_handoffs(self.layout), (operation.id,))
        envelope, _path = read_handoff(self.layout, operation.id)
        self.assertEqual(envelope.payload.version, "1.0.0")
        self.assertEqual(
            envelope.payload.manifest_digest,
            plan.impacts["release"]["manifest_digest"],
        )
        self.assertTrue(
            (self.layout.manager_versions / "1.0.0").is_dir()
        )
        self.assertFalse(
            (self.layout.root / "manager" / "current.json").exists()
        )
        self.assertIsNone(
            self.application.store.accepted_release(
                "pandrator-manager"
            )
        )
        self.assertEqual(
            self.application.store.configuration_revision(),
            0,
        )
        engine._recover_interrupted()
        self.assertEqual(
            self.application.store.get_operation(operation.id).state,
            OperationState.HANDOFF_PENDING,
        )
        with self.assertRaises(ConflictError):
            self.application.store.request_cancellation(operation.id)

    def test_handoff_directory_rejects_non_directory_redirection(self):
        directory = handoff_module.handoff_directory(self.layout)
        directory.write_text("not a directory", encoding="utf-8")

        with self.assertRaisesRegex(Exception, "not a real directory"):
            pending_handoffs(self.layout)

    def test_failed_handoff_launch_rolls_back_prepared_manager_slot(self):
        plan = self._prepare_native_manager_plan()
        operation = self._submit_manager_plan(plan)

        def fail_launch(_execution, _result):
            raise RuntimeError("helper could not start")

        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            release_authority=self.application.release_authority,
            manager_handoff_callback=fail_launch,
        )
        engine._execute(operation.id)
        failed = self.application.store.get_operation(operation.id)
        self.assertEqual(failed.state, OperationState.FAILED)
        self.assertFalse(
            (self.layout.manager_versions / "1.0.0").exists()
        )
        self.assertEqual(pending_handoffs(self.layout), ())

    def test_external_handoff_success_commits_only_after_new_health(self):
        plan, operation = self._prepare_pending_manager_handoff()
        launched = _FakeLaunchedProcess()
        with (
            mock.patch.object(handoff_module, "protect_path"),
            mock.patch.object(
                handoff_module,
                "_wait_for_old_manager",
            ),
            mock.patch.object(
                handoff_module,
                "_probe_new_runtime",
            ),
            mock.patch.object(
                handoff_module.subprocess,
                "Popen",
                return_value=launched,
            ),
            mock.patch.object(
                handoff_module,
                "_wait_for_new_manager",
            ) as health,
        ):
            result = handoff_module.run_handoff(
                self.layout.workspace,
                operation.id,
            )
        self.assertEqual(result, 0)
        health.assert_called_once()
        completed = self.application.store.get_operation(operation.id)
        self.assertEqual(completed.state, OperationState.SUCCEEDED)
        accepted = self.application.store.accepted_release(
            "pandrator-manager"
        )
        self.assertEqual(accepted["version"], "1.0.0")
        self.assertEqual(
            accepted["manifest_digest"],
            plan.impacts["release"]["manifest_digest"],
        )
        pointer = json.loads(
            (
                self.layout.root / "manager" / "current.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["version"], "1.0.0")
        self.assertEqual(pending_handoffs(self.layout), ())
        self.assertEqual(
            self.application.store.configuration_revision(),
            1,
        )

    def test_external_handoff_health_failure_restores_old_manager_state(self):
        _plan, operation = self._prepare_pending_manager_handoff()
        launched = _FakeLaunchedProcess()
        restart = mock.Mock()
        with (
            mock.patch.object(handoff_module, "protect_path"),
            mock.patch.object(
                handoff_module,
                "_wait_for_old_manager",
            ),
            mock.patch.object(
                handoff_module,
                "_probe_new_runtime",
            ),
            mock.patch.object(
                handoff_module.subprocess,
                "Popen",
                return_value=launched,
            ),
            mock.patch.object(
                handoff_module,
                "_wait_for_new_manager",
                side_effect=RuntimeError("new daemon unhealthy"),
            ),
            mock.patch.object(
                handoff_module,
                "_restart_previous_manager",
                restart,
            ),
        ):
            result = handoff_module.run_handoff(
                self.layout.workspace,
                operation.id,
            )
        self.assertEqual(result, 2)
        self.assertTrue(launched.terminated)
        restart.assert_called_once()
        failed = self.application.store.get_operation(operation.id)
        self.assertEqual(failed.state, OperationState.FAILED)
        self.assertEqual(failed.error_code, "manager_handoff_failed")
        self.assertIsNone(
            self.application.store.accepted_release(
                "pandrator-manager"
            )
        )
        self.assertEqual(
            self.application.store.configuration_revision(),
            0,
        )
        self.assertFalse(
            (self.layout.root / "manager" / "current.json").exists()
        )
        self.assertFalse(
            (self.layout.manager_versions / "1.0.0").exists()
        )
        self.assertEqual(pending_handoffs(self.layout), ())


if __name__ == "__main__":
    unittest.main()
