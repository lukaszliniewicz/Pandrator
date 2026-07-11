import json
import unittest
from pathlib import Path

from pandrator.web.openapi import build_openapi_document


class WebContractTests(unittest.TestCase):
    def test_committed_openapi_matches_python_contract_and_types_exist(self):
        root = Path(__file__).resolve().parents[1]
        committed = json.loads((root / "openapi.json").read_text(encoding="utf-8"))
        self.assertEqual(committed, build_openapi_document())
        generated = root / "web" / "src" / "lib" / "api.generated.ts"
        self.assertTrue(generated.is_file())
        self.assertIn("createProviderModel", generated.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
