"""Exercise archive/model staging with native verification and subprocesses."""

import hashlib
import json
import os
import tempfile
import unittest
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest import mock

from pandrator_manager.application import create_application
from pandrator_manager.artifacts import ArtifactDownloader
from pandrator_manager.components.audiocpp import (
    ASSETS,
    MODEL_PACKAGES,
    AudioCppAsset,
    resolve_assets,
)
from pandrator_manager.context import CancellationToken
from pandrator_manager.models import ComputeVariant, DesiredComponentState, OperationKind
from pandrator_manager.operations.handlers import FilesystemTaskHandler, OperationTaskContext


class NativeModelStagingTests(unittest.TestCase):
    def _fixture(self, *, corrupt_output: bool = False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        application = create_application(base / "workspace")
        definition = application.registry.definition("audio_cpp")
        original_assets, _ = resolve_assets(application.context, ComputeVariant.CPU, definition)
        key = next(key for key, value in ASSETS.items() if value is original_assets)
        contents = b"native subprocess fixture model\n"
        original_package = MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"]
        package = replace(
            original_package,
            target_directory="fixture-model",
            files=("fixture.gguf",),
            sha256=(hashlib.sha256(contents).hexdigest(),),
        )
        package_patch = mock.patch.dict(MODEL_PACKAGES, {package.id: package})
        package_patch.start()
        self.addCleanup(package_patch.stop)
        output_bytes = b"corrupt model" if corrupt_output else contents
        script = (
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"assert sys.argv[1:3] == ['install', {package.id!r}]\n"
            "spec = json.loads(next(Path('model_specs').glob('*.json')).read_text())\n"
            "download = spec['packages'][0]['download']\n"
            f"assert download['repo'] == {package.repository!r}\n"
            f"assert download['revision'] == {package.revision!r}\n"
            "root = Path(sys.argv[sys.argv.index('--models-root') + 1])\n"
            f"target = root / {package.target_directory!r}\n"
            "target.mkdir(parents=True, exist_ok=True)\n"
            f"(target / 'fixture.gguf').write_bytes({output_bytes!r})\n"
            f"(target / {package.marker_path(Path('models')).name!r}).write_text("
            "json.dumps({'fixture_installer': True}))\n"
            "log = Path('fixture-invocations.json')\n"
            "entries = json.loads(log.read_text()) if log.exists() else []\n"
            "entries.append({'argv': sys.argv[1:], 'ca': os.environ['SSL_CERT_FILE'], "
            "'download': download})\n"
            "log.write_text(json.dumps(entries))\n"
        )
        archive = base / "fixture-audiocpp.zip"
        server = "audiocpp_server.exe" if os.name == "nt" else "audiocpp_server"
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(server, "fixture server: not executed")
            output.writestr("tools/model_manager_v2.py", script)
            output.writestr(
                f"model_specs/{package.family}.json",
                json.dumps({"packages": [{"id": package.id, "download": {"revision": "main"}}]}),
            )
        archive_bytes = archive.read_bytes()
        asset = AudioCppAsset(
            archive.name, hashlib.sha256(archive_bytes).hexdigest(), ComputeVariant.CPU
        )
        assets_patch = mock.patch.dict(ASSETS, {key: (asset,)})
        assets_patch.start()
        self.addCleanup(assets_patch.stop)
        cache = application.context.layout.cache / "artifacts" / "audio_cpp" / asset.name
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(archive_bytes)
        network_patch = mock.patch.object(
            ArtifactDownloader, "_open_https", side_effect=AssertionError("Unexpected network")
        )
        network = network_patch.start()
        self.addCleanup(network_patch.stop)
        plan = application.plan(
            kind=OperationKind.INSTALL,
            desired={
                "audio_cpp": DesiredComponentState(
                    compute=ComputeVariant.CPU, options={"models": [package.id]}
                )
            },
        )
        operation, created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        execution = OperationTaskContext(
            context=application.context,
            store=application.store,
            registry=application.registry,
            supervisor=None,
            operation=operation,
            plan=plan,
            prior_results={},
            cancellation=CancellationToken(),
        )
        task = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
        return execution, task, package, network

    def test_native_model_install_rechecks_digests_and_repairs_staging(self) -> None:
        execution, task, package, network = self._fixture()
        handler = FilesystemTaskHandler()
        first = handler.execute(execution, task)
        target = Path(first["staged_path"])
        self.assertFalse(first["reused"])
        log = target / "fixture-invocations.json"
        invocation = json.loads(log.read_text())
        self.assertEqual(1, len(invocation))
        self.assertTrue(Path(invocation[0]["ca"]).is_file())
        self.assertEqual(package.revision, invocation[0]["download"]["revision"])
        model = package.required_paths(target / "models")[0]
        self.assertEqual(package.sha256[0], hashlib.sha256(model.read_bytes()).hexdigest())
        metadata = json.loads(package.marker_path(target / "models").read_text())
        self.assertTrue(metadata["fixture_installer"])
        self.assertTrue(metadata["provenance"]["digest_verified"])
        self.assertEqual(package.revision, metadata["provenance"]["requested_revision"])
        installation = json.loads((target / "install.json").read_text())
        self.assertEqual({package.id: package.revision}, installation["model_revisions"])
        repeated = handler.execute(execution, task)
        self.assertTrue(repeated["reused"])
        self.assertEqual(first["revision"], repeated["revision"])
        self.assertEqual(invocation, json.loads(log.read_text()))
        model.write_bytes(b"tampered after staging")
        repaired = handler.execute(execution, task)
        self.assertFalse(repaired["reused"])
        self.assertEqual(package.sha256[0], hashlib.sha256(model.read_bytes()).hexdigest())
        self.assertEqual(1, len(json.loads(log.read_text())))
        network.assert_not_called()

    def test_native_installer_corrupt_output_is_rejected_before_completion(self) -> None:
        execution, task, _package, network = self._fixture(corrupt_output=True)
        with self.assertRaisesRegex(RuntimeError, "failed SHA-256 verification"):
            FilesystemTaskHandler().execute(execution, task)
        target = execution.context.layout.staging / execution.operation.id / "audio_cpp" / "source"
        self.assertFalse((target / "install.json").exists())
        self.assertFalse((target / "server.json").exists())
        network.assert_not_called()
