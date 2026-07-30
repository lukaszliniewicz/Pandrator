import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import certifi
from dulwich.client import default_urllib3_manager

from pandrator_manager.errors import ManagerError
from pandrator_manager.operations.handlers import (
    _is_tls_verification_error,
)
from pandrator_manager.tls import (
    dulwich_config_with_ca,
    select_ca_bundle,
)


class ManagerTLSConfigurationTests(unittest.TestCase):
    def test_dulwich_pool_uses_selected_ca_bundle(self):
        configured = str(Path(certifi.where()).resolve())
        config, selection = dulwich_config_with_ca(
            {"PANDRATOR_CA_BUNDLE": configured}
        )

        pool = default_urllib3_manager(
            config,
            base_url="https://github.com/lukaszliniewicz/Pandrator.git",
        )

        self.assertEqual(selection.path, Path(configured))
        self.assertEqual(
            pool.connection_pool_kw["ca_certs"],
            configured,
        )
        self.assertEqual(
            pool.connection_pool_kw["cert_reqs"],
            "CERT_REQUIRED",
        )

    def test_system_bundle_precedes_packaged_certifi(self):
        selected = select_ca_bundle(
            {},
            system_candidates=(Path(certifi.where()),),
        )

        self.assertEqual(selected.source, "system")
        self.assertEqual(selected.path, Path(certifi.where()).resolve())

    def test_default_selection_honors_process_environment(self):
        configured = str(Path(certifi.where()).resolve())
        with mock.patch.dict(
            os.environ,
            {"PANDRATOR_CA_BUNDLE": configured},
            clear=False,
        ):
            selected = select_ca_bundle()

        self.assertEqual(
            selected.source,
            "environment:PANDRATOR_CA_BUNDLE",
        )
        self.assertEqual(selected.path, Path(configured))

    def test_invalid_explicit_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            invalid = Path(raw) / "invalid.pem"
            invalid.write_text("not a certificate", encoding="utf-8")

            with self.assertRaises(ManagerError) as raised:
                select_ca_bundle({"SSL_CERT_FILE": str(invalid)})

        self.assertEqual(raised.exception.code, "invalid_ca_bundle")
        self.assertEqual(
            raised.exception.details["environment_key"],
            "SSL_CERT_FILE",
        )

    def test_nested_certificate_failures_are_recognized(self):
        inner = ssl.SSLCertVerificationError(
            "unable to get local issuer certificate"
        )
        outer = RuntimeError("clone failed", inner)

        self.assertTrue(_is_tls_verification_error(outer))
        self.assertFalse(
            _is_tls_verification_error(RuntimeError("connection reset"))
        )


if __name__ == "__main__":
    unittest.main()
