"""Runtime action admission is limited to registered service identities."""

from __future__ import annotations

import tempfile
import unittest
from unittest import mock

from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.models import ManagedService
from pandrator_manager.runtime_specs import (
    PANDRATOR_API_SERVICE,
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_WORKER_SERVICE,
)
from pandrator_manager.supervisor import ProcessSupervisor


def managed_service(service_id: str) -> ManagedService:
    return ManagedService(
        id=service_id,
        component_id="fixture",
        service_key=service_id,
    )


class RuntimeIdentityAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.application = create_application(self.temporary.name)
        self.application.instance_id = "runtime-identity-test"
        self.supervisor = ProcessSupervisor(
            self.application.context,
            self.application.store,
            manager_instance_id="runtime-identity-test",
        )
        self.secret = "r" * 43
        self.client = create_api(
            self.application,
            self.supervisor,
            client_secret=self.secret,
        ).test_client()
        self.request_number = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def post_action(
        self,
        action: str,
        service_ids: list[str],
    ):
        self.request_number += 1
        return self.client.post(
            f"/v1/runtime/{action}",
            headers={
                "Authorization": f"Bearer {self.secret}",
                "Idempotency-Key": f"runtime-identity-{self.request_number}",
            },
            json={"service_ids": service_ids},
        )

    def test_unknown_ids_are_rejected_for_every_action_with_known_ids(self):
        service_ids = [
            PANDRATOR_API_SERVICE,
            PANDRATOR_MCP_SERVICE,
            PANDRATOR_WORKER_SERVICE,
            "tts.audio_cpp",
        ]
        services = [managed_service(service_id) for service_id in service_ids]
        with (
            mock.patch.object(
                self.supervisor,
                "snapshot",
                return_value=services,
            ),
            mock.patch.multiple(
                self.supervisor,
                start=mock.DEFAULT,
                stop=mock.DEFAULT,
                restart=mock.DEFAULT,
            ) as actions,
            mock.patch.object(
                self.application.context.event_sink,
                "emit",
            ) as emit,
        ):
            for action in ("start", "stop", "restart"):
                with self.subTest(action=action):
                    response = self.post_action(action, ["audio_cpp"])

                    self.assertEqual(404, response.status_code)
                    error = response.get_json()["error"]
                    self.assertEqual("not_found", error["code"])
                    self.assertIn("GET /v1/services", error["message"])
                    self.assertEqual(
                        ["audio_cpp"],
                        error["details"]["unknown_service_ids"],
                    )
                    self.assertEqual(
                        sorted(service_ids),
                        error["details"]["known_service_ids"],
                    )

        for action in ("start", "stop", "restart"):
            actions[action].assert_not_called()
        emit.assert_not_called()

    def test_mixed_known_and_unknown_ids_dispatch_nothing_for_every_action(self):
        service = managed_service("tts.audio_cpp")
        with (
            mock.patch.object(
                self.supervisor,
                "snapshot",
                return_value=[service],
            ),
            mock.patch.multiple(
                self.supervisor,
                start=mock.DEFAULT,
                stop=mock.DEFAULT,
                restart=mock.DEFAULT,
            ) as actions,
            mock.patch.object(
                self.application.context.event_sink,
                "emit",
            ) as emit,
        ):
            for action in ("start", "stop", "restart"):
                with self.subTest(action=action):
                    response = self.post_action(
                        action,
                        ["tts.audio_cpp", "missing.service"],
                    )
                    self.assertEqual(404, response.status_code)
                    self.assertEqual(
                        ["missing.service"],
                        response.get_json()["error"]["details"][
                            "unknown_service_ids"
                        ],
                    )

        for action in ("start", "stop", "restart"):
            actions[action].assert_not_called()
        emit.assert_not_called()

    def test_unknown_mixed_with_pandrator_service_skips_install_check_and_refresh(self):
        services = [
            managed_service(PANDRATOR_API_SERVICE),
            managed_service("tts.audio_cpp"),
        ]
        installed_component = [
            {
                "definition": {"id": "pandrator"},
                "inspection": {"state": "present"},
            }
        ]
        with (
            mock.patch.object(
                self.supervisor,
                "snapshot",
                return_value=services,
            ),
            mock.patch.object(
                self.application,
                "list_components",
                return_value=installed_component,
            ) as list_components,
            mock.patch(
                "pandrator_manager.api.app.pandrator_runtime_specs",
                return_value=[],
            ) as runtime_specs,
            mock.patch.multiple(
                self.supervisor,
                start=mock.DEFAULT,
                restart=mock.DEFAULT,
            ) as actions,
            mock.patch.object(
                self.application.context.event_sink,
                "emit",
            ) as emit,
        ):
            for action in ("start", "restart"):
                with self.subTest(action=action):
                    response = self.post_action(
                        action,
                        [PANDRATOR_API_SERVICE, "missing.service"],
                    )
                    self.assertEqual(404, response.status_code)

        list_components.assert_not_called()
        runtime_specs.assert_not_called()
        actions["start"].assert_not_called()
        actions["restart"].assert_not_called()
        emit.assert_not_called()

    def test_registered_ids_keep_existing_actions_and_default_all_behavior(self):
        service_ids = ["tts.audio_cpp", "fixture.second"]
        services = [managed_service(service_id) for service_id in service_ids]
        services_by_id = {service.id: service for service in services}
        with (
            mock.patch.object(
                self.supervisor,
                "snapshot",
                return_value=services,
            ),
            mock.patch.multiple(
                self.supervisor,
                start=mock.DEFAULT,
                stop=mock.DEFAULT,
                restart=mock.DEFAULT,
            ) as actions,
        ):
            for action in ("start", "stop", "restart"):
                actions[action].side_effect = services_by_id.__getitem__
                with self.subTest(action=action):
                    response = self.post_action(action, [])
                    self.assertEqual(200, response.status_code)
                    self.assertEqual(
                        service_ids,
                        [item["id"] for item in response.get_json()["items"]],
                    )
                    self.assertEqual(
                        [mock.call(service_id) for service_id in service_ids],
                        actions[action].call_args_list,
                    )
                    actions[action].reset_mock()

    def test_registered_pandrator_and_audio_cpp_ids_remain_valid(self):
        service_ids = [
            PANDRATOR_MCP_SERVICE,
            PANDRATOR_API_SERVICE,
            PANDRATOR_WORKER_SERVICE,
            "tts.audio_cpp",
        ]
        services = [managed_service(service_id) for service_id in service_ids]
        services_by_id = {service.id: service for service in services}
        with (
            mock.patch.object(
                self.supervisor,
                "snapshot",
                return_value=services,
            ),
            mock.patch.object(
                self.supervisor,
                "stop",
                side_effect=services_by_id.__getitem__,
            ) as stop,
        ):
            response = self.post_action("stop", service_ids)

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            service_ids,
            [item["id"] for item in response.get_json()["items"]],
        )
        self.assertEqual(
            [mock.call(service_id) for service_id in service_ids],
            stop.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
