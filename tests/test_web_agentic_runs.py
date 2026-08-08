import tempfile
import unittest

from sqlalchemy import select

from pandrator.web.agentic_runs import AgenticRunStore, stable_payload_hash
from pandrator.web.context_budget import ContextBudgetService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.knowledge import KnowledgeLedgerStore, KnowledgeValidationError
from pandrator.web.models import (
    AgentRun,
    Artifact,
    Job,
    Provider,
    ProviderModel,
    SessionRecord,
    utcnow,
)
from tests.web_test_support import prepare_web_test_data_root


class AgenticRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(paths.database)
        with self.database.session() as session:
            record = SessionRecord(name="Resume me", storage_key="resume-store")
            session.add(record)
            session.flush()
            self.session_id = record.id
            artifact = Artifact(
                session_id=record.id,
                kind="srt",
                role="transcription",
                relative_path="sessions/resume-store/source.srt",
                content_hash="source-hash",
            )
            session.add(artifact)
            session.flush()
            self.artifact_id = artifact.id

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _artifact(self):
        with self.database.session() as session:
            artifact = session.get(Artifact, self.artifact_id)
            session.expunge(artifact)
            return artifact

    def test_failed_run_reuses_only_matching_completed_units(self):
        store = AgenticRunStore(self.database)
        settings_hash = stable_payload_hash({"model": "test"})
        started = store.start(
            kind="correction",
            session_id=self.session_id,
            source_artifact=self._artifact(),
            settings_hash=settings_hash,
            settings={"model": "test"},
            job_id=None,
        )
        self.assertFalse(started.resumed)
        store.checkpoint(
            started.id,
            unit_key="block:0",
            ordinal=0,
            input_value={"cues": [1, 2]},
            output={"segments": [{"id": 1, "text": "Corrected"}]},
        )
        store.fail(started.id, "quota")

        resumed = store.start(
            kind="correction",
            session_id=self.session_id,
            source_artifact=self._artifact(),
            settings_hash=settings_hash,
            settings={"model": "test"},
            job_id=None,
        )

        self.assertTrue(resumed.resumed)
        self.assertEqual(
            resumed.completed_units["block:0"]["segments"][0]["text"],
            "Corrected",
        )

    def test_checkpoint_rejects_input_drift(self):
        store = AgenticRunStore(self.database)
        started = store.start(
            kind="translation",
            session_id=self.session_id,
            source_artifact=self._artifact(),
            settings_hash="settings",
            settings={},
            job_id=None,
        )
        store.checkpoint(
            started.id,
            unit_key="block:0",
            ordinal=0,
            input_value={"text": "one"},
            output={"text": "eins"},
        )
        with self.assertRaisesRegex(ValueError, "input drifted"):
            store.checkpoint(
                started.id,
                unit_key="block:0",
                ordinal=0,
                input_value={"text": "different"},
                output={"text": "anders"},
            )

    def test_prepare_resume_claims_the_run_before_enqueuing(self):
        queue = JobQueue(self.database)
        job = queue.enqueue(
            "subtitle.correct",
            {"session_id": self.session_id},
            session_id=self.session_id,
        )
        store = AgenticRunStore(self.database)
        started = store.start(
            kind="correction",
            session_id=self.session_id,
            source_artifact=self._artifact(),
            settings_hash="settings",
            settings={},
            job_id=job.id,
        )
        store.fail(started.id, "quota")

        resumed, previous_job = store.prepare_resume(started.id)

        self.assertEqual(job.id, previous_job.id)
        self.assertEqual("retrying", resumed.status)
        with self.assertRaisesRegex(ValueError, "failed or interrupted"):
            store.prepare_resume(started.id)

    def test_startup_reconciliation_makes_crashed_agent_run_resumable(self):
        queue = JobQueue(self.database)
        job = queue.enqueue(
            "subtitle.translate",
            {"session_id": self.session_id},
            session_id=self.session_id,
        )
        with self.database.session() as session:
            managed_job = session.get(Job, job.id)
            managed_job.status = "failed"
            managed_job.error_message = "worker disappeared"
            managed_job.finished_at = utcnow()
            session.add(
                AgentRun(
                    kind="translation",
                    session_id=self.session_id,
                    source_artifact_id=self.artifact_id,
                    job_id=job.id,
                    status="running",
                    source_content_hash="source-hash",
                    settings_hash="settings",
                )
            )

        queue.reconcile()

        with self.database.session() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.job_id == job.id))
            self.assertEqual("failed", run.status)
            self.assertEqual("worker disappeared", run.error_message)

    def test_locked_manual_glossary_wins_over_research(self):
        store = KnowledgeLedgerStore(self.database)
        manual = store.merge_glossary(
            self.session_id,
            source_language="de",
            target_language="en",
            entries=[{"source": "Bundestag", "target": "Bundestag"}],
            origin="manual",
            locked=True,
        )
        research = store.merge_glossary(
            self.session_id,
            source_language="de",
            target_language="en",
            entries=[{"source": "Bundestag", "target": "Federal Diet"}],
            origin="research",
        )

        self.assertGreater(research["revision"], manual["revision"])
        self.assertEqual(
            research["payload"]["entries"][0]["target"],
            "Bundestag",
        )
        self.assertEqual(
            research["payload"]["conflicts"][0]["rejected"],
            "Federal Diet",
        )

    def test_context_batches_respect_eighty_percent_default(self):
        budget = ContextBudgetService(self.database).resolve(
            "unknown/model",
            fixed_prompt="instructions",
        )
        self.assertEqual(budget.context_window_tokens, 262_144)
        self.assertEqual(budget.fraction, 0.8)
        batches = ContextBudgetService.partition(
            ({"id": index, "text": "Wort " * 20_000} for index in range(20)),
            model="unknown/model",
            budget_tokens=budget.input_budget_tokens,
        )
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(batch) for batch in batches), 20)

    def test_context_budget_preserves_slashes_in_custom_provider_model_ids(self):
        with self.database.session() as session:
            provider = Provider(
                kind="llm",
                provider_key="openai-compatible",
                label="Test provider",
            )
            session.add(provider)
            session.flush()
            session.add(
                ProviderModel(
                    provider_id=provider.id,
                    model_id="organization/model-name",
                    context_window_tokens=131_072,
                    max_output_tokens=4_096,
                )
            )
            provider_id = provider.id

        budget = ContextBudgetService(self.database).resolve(
            f"custom:{provider_id}/organization/model-name"
        )

        self.assertEqual(131_072, budget.context_window_tokens)
        self.assertEqual(4_096, budget.max_output_tokens)

    def test_knowledge_replace_rejects_malformed_glossary(self):
        store = KnowledgeLedgerStore(self.database)
        ledger = store.get(
            self.session_id,
            "glossary",
            source_language="de",
            target_language="en",
        )
        with self.assertRaisesRegex(KnowledgeValidationError, "source and target"):
            store.replace(
                ledger["id"],
                ledger["revision"],
                {"entries": [{"source": "Bundestag"}], "conflicts": []},
            )


if __name__ == "__main__":
    unittest.main()
