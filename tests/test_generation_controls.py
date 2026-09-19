import tempfile
import unittest
from pathlib import Path

from sqlalchemy import func, select

from pandrator.web.database import Database
from pandrator.web.generation_controls import (
    RevisionConflict,
    get_generation_controls,
    merge_character_proposals,
    resolve_cast_voice,
    save_generation_controls,
)
from pandrator.web.models import KnowledgeLedger, Voice
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


class GenerationControlsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        paths = prepare_web_test_data_root(Path(self.temporary.name))
        self.database = Database(paths.database)
        self.record = SessionService(self.database).create("Controls")

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_get_is_read_only_and_revision_checked(self):
        with self.database.session() as session:
            before = session.scalar(
                select(func.count(KnowledgeLedger.id)).where(
                    KnowledgeLedger.session_id == self.record.id
                )
            )
            initial = get_generation_controls(session, self.record.id)
            after = session.scalar(
                select(func.count(KnowledgeLedger.id)).where(
                    KnowledgeLedger.session_id == self.record.id
                )
            )
        self.assertEqual(0, before)
        self.assertEqual(0, after)
        self.assertEqual(0, initial["revision"])
        self.assertIsNone(initial["id"])

        with self.database.session() as session:
            saved = save_generation_controls(
                session,
                self.record.id,
                expected_revision=0,
                characters=[{"display_name": "  Alice  "}],
            )
            self.assertEqual(1, saved["revision"])
            self.assertTrue(saved["characters"][0]["id"].startswith("c-"))
            with self.assertRaises(RevisionConflict):
                save_generation_controls(
                    session,
                    self.record.id,
                    expected_revision=0,
                    characters=[],
                )

        with self.database.session() as session:
            with self.assertRaises(KeyError):
                get_generation_controls(session, "missing-session")

    def test_rename_retains_id_and_locked_updates_need_unlock(self):
        with self.database.session() as session:
            first = save_generation_controls(
                session,
                self.record.id,
                expected_revision=0,
                characters=[{"id": "c-alice", "display_name": "Alice"}],
            )
            renamed = save_generation_controls(
                session,
                self.record.id,
                expected_revision=first["revision"],
                characters=[{"id": "c-alice", "display_name": "Alicia"}],
            )
            self.assertEqual("c-alice", renamed["characters"][0]["id"])
            locked = save_generation_controls(
                session,
                self.record.id,
                expected_revision=renamed["revision"],
                characters=[
                    {"id": "c-alice", "display_name": "Alicia", "locked": True}
                ],
            )
            with self.assertRaises(RevisionConflict):
                save_generation_controls(
                    session,
                    self.record.id,
                    expected_revision=locked["revision"],
                    characters=[{"id": "c-alice", "display_name": "Alice"}],
                )
            unlocked = save_generation_controls(
                session,
                self.record.id,
                expected_revision=locked["revision"],
                characters=[{"id": "c-alice", "display_name": "Alice"}],
                unlock_ids=["c-alice"],
            )
            self.assertEqual("Alice", unlocked["characters"][0]["display_name"])

    def test_cast_resolution_precedence_and_category_separation(self):
        cast = {
            "narrator": {"voice": "narrator"},
            "categories": {
                "female": {"voice": "female"},
                "androgynous": {"voice": "androgynous"},
            },
            "characters": {"c-alice": {"voice": "alice"}},
            "source_speakers": {"S1": {"voice": "source"}},
        }
        with self.database.session() as session:
            controls = save_generation_controls(
                session,
                self.record.id,
                expected_revision=0,
                characters=[
                    {
                        "id": "c-alice",
                        "display_name": "Alice",
                        "voice_category": "female",
                    }
                ],
                cast=cast,
            )

        self.assertEqual("span", resolve_cast_voice(controls, span_voice="span")["source"])
        self.assertEqual(
            "alice",
            resolve_cast_voice(controls, speaker_id="c-alice", dialogue=True)[
                "binding"
            ]["voice"],
        )
        self.assertEqual(
            "source",
            resolve_cast_voice(controls, source_speaker="S1", dialogue=True)[
                "binding"
            ]["voice"],
        )
        self.assertEqual(
            "female",
            resolve_cast_voice(controls, dialogue=True, voice_category="female")[
                "binding"
            ]["voice"],
        )
        self.assertEqual(
            "narrator", resolve_cast_voice(controls)["binding"]["voice"]
        )
        self.assertEqual(
            "inherited",
            resolve_cast_voice(
                {"characters": [], "cast": {"categories": {"androgynous": {"voice": "a"}}}},
                dialogue=True,
            )["source"],
        )
        with self.assertRaises(ValueError):
            resolve_cast_voice(controls, speaker_id="c-missing")

    def test_cast_references_and_managed_voice_ids_are_validated(self):
        with self.database.session() as session:
            with self.assertRaises(ValueError):
                save_generation_controls(
                    session,
                    self.record.id,
                    expected_revision=0,
                    characters=[{"id": "c-alice", "display_name": "Alice"}],
                    cast={"characters": {"c-missing": {"voice": "x"}}},
                )
            with self.assertRaises(ValueError):
                save_generation_controls(
                    session,
                    self.record.id,
                    expected_revision=0,
                    cast={"narrator": {"voice_id": "voice-missing"}},
                )
            session.add(Voice(id="voice-known", name="Known"))
            valid = save_generation_controls(
                session,
                self.record.id,
                expected_revision=0,
                cast={"narrator": {"voice_id": "voice-known"}},
            )
            self.assertEqual("voice-known", valid["cast"]["narrator"]["voice_id"])

    def test_proposals_are_namesake_safe_and_idempotent(self):
        with self.database.session() as session:
            initial = save_generation_controls(
                session,
                self.record.id,
                expected_revision=0,
                characters=[
                    {"id": "c-one", "display_name": "Alex"},
                    {"id": "c-two", "display_name": "Alex"},
                ],
            )
            proposal = merge_character_proposals(
                session,
                self.record.id,
                [{"id": "model-alex", "display_name": "Robin"}],
                origin="model",
            )
            self.assertEqual(initial["revision"] + 1, proposal["revision"])
            repeat = merge_character_proposals(
                session,
                self.record.id,
                [{"id": "model-alex", "display_name": "Robin"}],
                origin="model",
            )
            self.assertEqual(proposal["revision"], repeat["revision"])
            self.assertEqual("proposed", repeat["characters"][-1]["status"])
            with self.assertRaises(ValueError):
                merge_character_proposals(
                    session,
                    self.record.id,
                    [{"id": "model-other", "display_name": "Alex"}],
                    origin="model",
                )
            with self.assertRaises(RevisionConflict):
                merge_character_proposals(
                    session,
                    self.record.id,
                    [{"id": "model-alex", "display_name": "Different"}],
                    origin="model",
                )


if __name__ == "__main__":
    unittest.main()
