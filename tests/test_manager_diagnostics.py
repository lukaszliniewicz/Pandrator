import io
import json
import tempfile
import unittest
import uuid
import zipfile

from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.models import (
    DesiredComponentState,
    OperationKind,
    OperationState,
    TaskState,
)
from pandrator_manager.supervisor import ProcessSupervisor


class ManagerDiagnosticBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.application = create_application(self.temporary.name)
        self.application.instance_id = "diagnostic-test"
        self.supervisor = ProcessSupervisor(
            self.application.context,
            self.application.store,
            manager_instance_id="diagnostic-test",
        )
        self.secret = "s" * 43
        self.api = create_api(
            self.application,
            self.supervisor,
            client_secret=self.secret,
        )
        self.client = self.api.test_client()
        self.auth = {"Authorization": f"Bearer {self.secret}"}

    def _failed_operation(self):
        plan = self.application.plan(
            kind=OperationKind.INSTALL,
            desired={"silero": DesiredComponentState()},
        )
        operation, _created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=str(uuid.uuid4()),
        )
        task = self.application.store.operation_tasks(operation.id)[0]
        self.application.store.update_operation_task(
            operation.id,
            task.task.id,
            state=TaskState.ROLLED_BACK,
            attempt=1,
            error={
                "code": "fixture_failure",
                "message": "access_token=do-not-share",
            },
        )
        operation.state = OperationState.FAILED
        operation.error_code = "fixture_failure"
        operation.error_message = (
            "Download failed at "
            "https://person:password@example.test/repo?token=do-not-share"
        )
        self.application.store.update_operation(operation)
        return operation

    def test_authenticated_bundle_is_bounded_and_redacted(self):
        operation = self._failed_operation()
        workspace = str(self.application.context.layout.workspace)
        log = self.application.context.layout.logs / "manager.log"
        log.write_text(
            "\n".join(
                (
                    f"workspace={workspace}",
                    "Authorization: Bearer private-token",
                    (
                        "source=https://person:password@example.test/"
                        "repo?token=query-secret"
                    ),
                )
            ),
            encoding="utf-8",
        )

        response = self.client.get(
            "/v1/diagnostics/bundle",
            headers=self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/zip")
        self.assertIn(
            "pandrator-diagnostics-",
            response.headers["Content-Disposition"],
        )
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            names = set(archive.namelist())
            self.assertIn("README.txt", names)
            self.assertIn("summary.json", names)
            self.assertIn("logs/manager.log", names)
            self.assertFalse(
                any("sqlite" in name or "secret" in name for name in names)
            )
            combined = b"\n".join(
                archive.read(name)
                for name in sorted(names)
            ).decode("utf-8")
            summary = json.loads(archive.read("summary.json"))

        self.assertNotIn(workspace, combined)
        self.assertNotIn("do-not-share", combined)
        self.assertNotIn("private-token", combined)
        self.assertNotIn("query-secret", combined)
        self.assertNotIn("person:password", combined)
        self.assertIn("$WORKSPACE", combined)
        self.assertIn("Authorization: <redacted>", combined)
        self.assertEqual(
            summary["operations"][0]["id"],
            operation.id,
        )
        self.assertEqual(
            summary["operations"][0]["tasks"][0]["error"]["code"],
            "fixture_failure",
        )
        self.assertEqual(
            summary["operations"][0]["tasks"][0]["error"]["message"],
            "access_token=<redacted>",
        )

    def test_bundle_requires_authentication(self):
        response = self.client.get("/v1/diagnostics/bundle")

        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
