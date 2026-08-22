import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.pronunciations import (
    PronunciationLibrary,
    apply_reviewed_pronunciations,
    render_respelling,
    validate_respelling,
)
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


class PronunciationLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        service = SessionService(self.database)
        self.first = service.create("First", workflow_kind="audiobook")
        self.second = service.create("Second", workflow_kind="audiobook")
        self.library = PronunciationLibrary(self.database)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_reviewed_session_entry_overrides_global_and_proposals_are_inactive(self):
        global_entry = self.library.create(
            {
                "source_form": "Imaoka",
                "phonetic": "ee-mah-oh-kah",
                "language": "en",
                "scope": "global",
                "status": "reviewed",
            }
        )
        proposed = self.library.propose(
            session_id=self.first.id,
            source_form="Imaoka",
            phonetic="ih-mah-oh-kah",
            language="en",
        )

        unresolved = self.library.resolve(
            "Imaoka arrived.",
            session_id=self.first.id,
            language="en",
        )
        self.assertEqual([global_entry["id"]], [item["id"] for item in unresolved])

        reviewed = self.library.update(
            proposed["id"],
            proposed["revision"],
            {"status": "reviewed"},
        )
        first_result = self.library.resolve(
            "Imaoka arrived.",
            session_id=self.first.id,
            language="en",
        )
        second_result = self.library.resolve(
            "Imaoka arrived.",
            session_id=self.second.id,
            language="en",
        )
        self.assertEqual([reviewed["id"]], [item["id"] for item in first_result])
        self.assertEqual([global_entry["id"]], [item["id"] for item in second_result])
        self.assertEqual("ihmahohkah", render_respelling(reviewed["phonetic"]))

    def test_respelling_format_is_strict(self):
        with self.assertRaisesRegex(ValueError, "lowercase Unicode"):
            self.library.create(
                {
                    "source_form": "Imaoka",
                    "phonetic": "Ee mah oh kah",
                    "language": "en",
                }
            )

    def test_unicode_lowercase_respellings_normalize_and_render(self):
        self.assertEqual("syms", validate_respelling("syms"))
        self.assertEqual("łys-kon-syn", validate_respelling("łys-kon-syn"))
        self.assertEqual("kołcz", validate_respelling("kołcz"))
        self.assertEqual("é", validate_respelling("e\u0301"))
        self.assertEqual("łyskonsyn", render_respelling("łys-kon-syn"))

        entry = self.library.create(
            {
                "source_form": "Wisconsin",
                "phonetic": "łys-kon-syn",
                "language": "en",
                "status": "reviewed",
            }
        )
        resolved = self.library.resolve(
            "Wisconsin",
            session_id=self.first.id,
            language="en",
        )
        self.assertEqual([entry["id"]], [item["id"] for item in resolved])
        self.assertEqual(
            "łyskonsyn", apply_reviewed_pronunciations("Wisconsin", resolved)
        )

    def test_respelling_rejects_non_lowercase_or_unsafe_separators(self):
        invalid_values = (
            "",
            "Łys",
            "łys2",
            "łys!",
            "łys_kon",
            "łys--kon",
            "-łys",
            "łys-",
            "łys  kon",
            " łys",
            "łys ",
            "łys\tkon",
            "łys\u200bkon",
            "猫",
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_respelling(value)

    def test_deterministic_application_is_bounded_longest_and_non_mutating(self):
        long_entry = self.library.create(
            {
                "source_form": "existential threat",
                "phonetic": "egzistenszial fret",
                "language": "en",
                "status": "reviewed",
            }
        )
        short_entry = self.library.create(
            {
                "source_form": "threat",
                "phonetic": "fret",
                "language": "en",
                "status": "reviewed",
            }
        )
        entries = self.library.resolve(
            "An existential threat, but not threats.",
            session_id=self.first.id,
            language="en",
        )

        self.assertEqual(
            [long_entry["id"], short_entry["id"]],
            [item["id"] for item in entries],
        )
        source = "An existential threat, but not threats."
        self.assertEqual(
            "An egzistenszial fret, but not threats.",
            apply_reviewed_pronunciations(source, entries),
        )
        self.assertEqual(source, "An existential threat, but not threats.")

    def test_deterministic_application_respects_language_backend_and_scope(self):
        global_entry = self.library.create(
            {
                "source_form": "route",
                "phonetic": "root",
                "language": "en",
                "backend": "xtts",
                "status": "reviewed",
            }
        )
        session_entry = self.library.create(
            {
                "source_form": "route",
                "phonetic": "rowt",
                "language": "en",
                "backend": "xtts",
                "scope": "session",
                "session_id": self.first.id,
                "status": "reviewed",
            }
        )
        self.assertEqual(
            session_entry["id"],
            self.library.resolve(
                "The route is clear.",
                session_id=self.first.id,
                language="en",
                backend="xtts",
            )[0]["id"],
        )
        self.assertEqual(
            global_entry["id"],
            self.library.resolve(
                "The route is clear.",
                session_id=self.second.id,
                language="en",
                backend="xtts",
            )[0]["id"],
        )
        self.assertEqual(
            [],
            self.library.resolve(
                "The route is clear.",
                session_id=self.first.id,
                language="pl",
                backend="xtts",
            ),
        )
        self.assertEqual(
            [],
            self.library.resolve(
                "The route is clear.",
                session_id=self.first.id,
                language="en",
                backend="chatterbox",
            ),
        )


class PronunciationApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_create_review_and_delete_pronunciation(self):
        created_response = self.client.post(
            "/api/v1/pronunciations",
            json={
                "source_form": "Imaoka",
                "phonetic": "ee-mah-oh-kah",
                "language": "en",
                "scope": "global",
                "status": "proposed",
            },
            headers=self.headers,
        )
        self.assertEqual(201, created_response.status_code)
        created = created_response.get_json()
        self.assertEqual(
            [created["id"]],
            [
                item["id"]
                for item in self.client.get(
                    "/api/v1/pronunciations?status=proposed"
                ).get_json()["items"]
            ],
        )

        reviewed_response = self.client.patch(
            f"/api/v1/pronunciations/{created['id']}",
            json={"status": "reviewed"},
            headers={
                **self.headers,
                "If-Match": f'"{created["revision"]}"',
            },
        )
        self.assertEqual(200, reviewed_response.status_code)
        reviewed = reviewed_response.get_json()
        self.assertEqual("reviewed", reviewed["status"])

        deleted = self.client.delete(
            f"/api/v1/pronunciations/{created['id']}",
            headers={
                **self.headers,
                "If-Match": f'"{reviewed["revision"]}"',
            },
        )
        self.assertEqual(204, deleted.status_code)

    def test_invalid_pronunciation_is_rejected(self):
        response = self.client.post(
            "/api/v1/pronunciations",
            json={
                "source_form": "Imaoka",
                "phonetic": "Ee mah oh kah",
                "language": "en",
            },
            headers=self.headers,
        )
        self.assertEqual(422, response.status_code)
        self.assertIn("lowercase Unicode", response.get_json()["error"]["message"])


if __name__ == "__main__":
    unittest.main()
