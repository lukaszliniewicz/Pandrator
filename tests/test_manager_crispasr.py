"""Native cached-runtime installation and offline review/execution boundaries."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest import mock

from pandrator_manager.application import create_application
from pandrator_manager.artifacts import ArtifactDownloader
from pandrator_manager.components.crispasr import (
    ASSETS,
    CRISPASR_VERSION,
    CrispASRAsset,
    resolve_asset,
)
from pandrator_manager.context import CancellationToken
from pandrator_manager.errors import ManagerError
from pandrator_manager.models import ComputeVariant, DesiredComponentState, OperationKind
from pandrator_manager.operations.handlers import FilesystemTaskHandler, OperationTaskContext


class CrispASROfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.application = create_application(base / "workspace")
        definition = self.application.registry.definition("crispasr")
        original, _ = resolve_asset(self.application.context, ComputeVariant.CPU, definition)
        key = next(key for key, value in ASSETS.items() if value is original)
        archive = base / "fixture-crispasr.zip"
        executable = "crispasr.exe" if os.name == "nt" else "crispasr"
        script = (
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            "Path('probe-invocation.json').write_text(json.dumps(sys.argv[1:]))\n"
            f"print('version       : {CRISPASR_VERSION}')\n"
        )
        with zipfile.ZipFile(archive, "w") as output:
            output.writestr(f"runtime/{executable}", script)
        self.archive_bytes = archive.read_bytes()
        self.asset = CrispASRAsset(
            archive.name,
            hashlib.sha256(self.archive_bytes).hexdigest(),
            ComputeVariant.CPU,
            ("cpu",),
        )
        assets_patch = mock.patch.dict(ASSETS, {key: self.asset})
        assets_patch.start()
        self.addCleanup(assets_patch.stop)
        self.cache = self.application.context.layout.cache / "artifacts" / "crispasr" / archive.name
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        network_patch = mock.patch.object(
            ArtifactDownloader,
            "_open_https",
            side_effect=AssertionError("Offline execution attempted HTTPS acquisition"),
        )
        self.network = network_patch.start()
        self.addCleanup(network_patch.stop)

    def _plan(self):
        return self.application.plan(
            kind=OperationKind.INSTALL,
            desired={
                "crispasr": DesiredComponentState(
                    compute=ComputeVariant.CPU, options={"offline": True}
                )
            },
        )

    def _review_and_submit(self):
        self.cache.write_bytes(self.archive_bytes)
        plan = self._plan()
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        execution = OperationTaskContext(
            context=self.application.context,
            store=self.application.store,
            registry=self.application.registry,
            supervisor=None,
            operation=operation,
            plan=plan,
            prior_results={},
            cancellation=CancellationToken(),
        )
        stage = next(task for task in plan.tasks if task.kind == "stage_crispasr")
        return execution, stage

    def test_offline_review_rejects_missing_or_tampered_runtime(self) -> None:
        for contents in (None, b"tampered runtime"):
            with self.subTest(contents=contents):
                if contents is not None:
                    self.cache.write_bytes(contents)
                with self.assertRaises(ManagerError) as caught:
                    self._plan()
                self.assertEqual("preflight_failed", caught.exception.code)
                details = caught.exception.details
                assert details is not None
                checks = details["checks"]
                self.assertTrue(any(item["code"] == "offline.crispasr" for item in checks))
        self.network.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Fixture probe is a native Unix executable script")
    def test_cached_runtime_is_hashed_extracted_probed_and_reused(self) -> None:
        execution, stage = self._review_and_submit()
        handler = FilesystemTaskHandler()
        result = handler.execute(execution, stage)
        target = Path(result["staged_path"])
        self.assertFalse(result["reused"])
        self.assertEqual(str(self.cache), result["asset_path"])
        self.assertEqual(["--version"], json.loads((target / "probe-invocation.json").read_text()))
        metadata = json.loads((target / "install.json").read_text())
        self.assertEqual(self.asset.sha256, metadata["sha256"])
        self.assertEqual(CRISPASR_VERSION, metadata["version"])
        self.assertEqual(self.asset.name, metadata["asset"])
        self.assertEqual("cpu", metadata["effective_backend"])
        self.assertTrue(stage.inputs["offline"])
        probe_mtime = (target / "probe-invocation.json").stat().st_mtime_ns
        repeated = handler.execute(execution, stage)
        self.assertTrue(repeated["reused"])
        self.assertEqual(result["revision"], repeated["revision"])
        self.assertEqual(str(target), repeated["staged_path"])
        self.assertEqual(probe_mtime, (target / "probe-invocation.json").stat().st_mtime_ns)
        self.network.assert_not_called()

    def test_cache_loss_after_review_does_not_enable_network(self) -> None:
        execution, stage = self._review_and_submit()
        self.cache.unlink()
        with self.assertRaises(FileNotFoundError):
            FilesystemTaskHandler().execute(execution, stage)
        self.network.assert_not_called()

    def test_legacy_resolved_offline_flag_does_not_enable_network(self) -> None:
        execution, stage = self._review_and_submit()
        legacy_inputs = dict(stage.inputs)
        legacy_inputs.pop("offline", None)
        legacy_stage = stage.model_copy(update={"inputs": legacy_inputs})
        self.cache.write_bytes(b"tampered after review")
        with self.assertRaises(FileNotFoundError):
            FilesystemTaskHandler().execute(execution, legacy_stage)
        self.network.assert_not_called()
