import re
import ssl
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import requests
from test_mcp_application_client import create_test_ca
from werkzeug.serving import make_server

from pandrator.web.api import create_app
from pandrator_mcp.credentials import (
    CredentialReference,
    CredentialResolver,
    SecretValue,
)
from pandrator_mcp.enrollment import enroll_target
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.targets import (
    TargetProfile,
    TargetRegistry,
    TargetStore,
)
from tests.web_test_support import prepare_web_test_data_root


class MemoryCredentialBackend:
    name = "keyring"

    def __init__(self):
        self.values = {}

    def resolve(self, reference: CredentialReference) -> SecretValue:
        return SecretValue(self.values[reference.reference])

    def store(
        self,
        reference: CredentialReference,
        value: SecretValue,
    ) -> None:
        self.values[reference.reference] = value.reveal()

    def delete(self, reference: CredentialReference) -> None:
        self.values.pop(reference.reference, None)


class McpEnrollmentTests(unittest.TestCase):
    def test_browser_pkce_enrollment_pins_identity_and_hides_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_root = root / "data"
            prepare_web_test_data_root(data_root)
            ca_path, certificate_path, key_path = create_test_ca(root)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(certificate_path, key_path)

            app = create_app(
                data_root=data_root,
                testing=True,
                trusted_hosts=["127.0.0.1"],
                public_origin="https://127.0.0.1:1",
            )
            app.extensions["pandrator"]["auth"].initialize_owner(
                "correct horse battery staple"
            )
            server = make_server(
                "127.0.0.1",
                0,
                app,
                threaded=True,
                ssl_context=tls,
            )
            origin = f"https://127.0.0.1:{server.server_port}"
            app.extensions["pandrator"][
                "identity"
            ].public_origin = origin
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            try:
                config = root / "targets.json"
                client_id = str(uuid.uuid4())
                profile = TargetProfile(
                    name="private",
                    mode=TargetMode.PRIVATE_NETWORK,
                    application_origin=origin,
                    allowed_private_cidrs=("127.0.0.0/8",),
                    ca_bundle=str(ca_path),
                    automation_client_id=client_id,
                    requested_application_scopes=(
                        "app.read",
                        "app.cancel",
                    ),
                )
                store = TargetStore(config)
                store.put(profile)
                registry = TargetRegistry(store.load())
                backend = MemoryCredentialBackend()
                credentials = CredentialResolver((backend,))

                def approve(url):
                    browser = requests.Session()
                    page = browser.get(
                        url,
                        verify=str(ca_path),
                        timeout=10,
                    )
                    nonce = re.search(
                        r'name="authorization_nonce" value="([^"]+)"',
                        page.text,
                    ).group(1)
                    consent = browser.post(
                        url,
                        data={
                            "authorization_nonce": nonce,
                            "decision": "approve",
                            "password": (
                                "correct horse battery staple"
                            ),
                        },
                        verify=str(ca_path),
                        allow_redirects=False,
                        timeout=10,
                    )
                    self.assertEqual(302, consent.status_code)
                    callback = browser.get(
                        consent.headers["Location"],
                        timeout=10,
                    )
                    self.assertEqual(200, callback.status_code)
                    return True

                with patch(
                    "pandrator_mcp.enrollment.webbrowser.open",
                    side_effect=approve,
                ):
                    summary = enroll_target(
                        profile=profile,
                        binding=registry.bind("private"),
                        store=store,
                        credentials=credentials,
                        scopes=("app.read", "app.cancel"),
                        expires_in_days=7,
                    )

                updated = store.load(missing_ok=False)[0]
                raw = next(iter(backend.values.values()))
                serialized = config.read_text(encoding="utf-8")
                self.assertEqual(client_id, summary.client_id)
                self.assertEqual(
                    summary.target_instance_id,
                    updated.expected_identity.application_instance_id,
                )
                self.assertEqual(
                    "automation:" + client_id,
                    updated.enrolled_subject,
                )
                self.assertTrue(raw.startswith("pan_"))
                self.assertNotIn(raw, serialized)
                self.assertNotIn(raw, repr(summary))

                rotated_profile = store.load(
                    missing_ok=False
                )[0]
                rotated_registry = TargetRegistry(
                    (rotated_profile,)
                )
                with patch(
                    "pandrator_mcp.enrollment.webbrowser.open",
                    side_effect=approve,
                ):
                    rotated = enroll_target(
                        profile=rotated_profile,
                        binding=rotated_registry.bind("private"),
                        store=store,
                        credentials=credentials,
                        scopes=("app.read", "app.cancel"),
                        expires_in_days=7,
                    )
                new_raw = next(iter(backend.values.values()))
                self.assertTrue(rotated.credential_rotated)
                self.assertNotEqual(raw, new_raw)
                old_response = requests.get(
                    f"{origin}/api/v1/system/identity",
                    headers={"Authorization": f"Bearer {raw}"},
                    verify=str(ca_path),
                    timeout=10,
                )
                new_response = requests.get(
                    f"{origin}/api/v1/system/identity",
                    headers={
                        "Authorization": f"Bearer {new_raw}"
                    },
                    verify=str(ca_path),
                    timeout=10,
                )
                self.assertEqual(401, old_response.status_code)
                self.assertEqual(200, new_response.status_code)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                app.extensions["pandrator"]["database"].dispose()


if __name__ == "__main__":
    unittest.main()
