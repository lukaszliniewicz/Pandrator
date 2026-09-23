"""Session-view performance guards: query budgets, no global caches, row-set equivalence.

These tests pin the profiled session-view bottlenecks without wall-clock
assertions:
- revision_history must not issue per-segment SQL (deferred-column N+1),
  including with performance/casting enabled;
- provider catalogue builds are request-scoped (a second identical read
  rebuilds) and bounded by distinct catalogue inputs, not by segments;
- the segment-driven takes/artifacts split counts exactly the inner-join
  row set, including adversarial take/artifact states;
- repair_state_hash stays deterministic across calls.
"""

import tempfile
import unittest
import uuid
import wave
from unittest.mock import patch

from sqlalchemy import event, func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import (
    IDENTITY_KEY,
    AudioIdentityContext,
)
from pandrator.web.generation_review import revision_history
from pandrator.web.models import Artifact, AudioTake, GenerationSegment


class SessionViewPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]
        self.database = self.services["database"]
        self.generation = self.services["generation"]
        self.addCleanup(self.database.dispose)

    def _create_session(self, name="Session-view perf"):
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": f"{name} {uuid.uuid4().hex[:8]}", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        return created.get_json()["id"]

    def _wave_path(self, label):
        directory = self.services["paths"].uploads / "session-view-perf"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{label}-{uuid.uuid4().hex}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 1600)
        return path

    def _plan(self, session_id, count, *, voices=("v-a", "v-b", None)):
        segments = []
        for index in range(count):
            voice = voices[index % len(voices)]
            segments.append(
                {
                    "text": f"Speech block number {index} with enough words.",
                    "language": "de" if index % 2 else "en",
                    **({"voice": voice} if voice else {}),
                    "speech_block_provenance": {
                        "schema_version": 1,
                        "source_reference_namespace": "subtitle_ordinal",
                        "source_cues": [
                            {
                                "reference": index,
                                "start_ms": index * 1000,
                                "end_ms": index * 1000 + 900,
                                "display_spans": [[0, 10]],
                                "speech_spans": [[0, 10]],
                            }
                        ],
                    },
                }
            )
        return self.generation.create_plan(
            session_id, source_revision_id=None, settings={}, segments=segments
        )

    def _seed_reusable_takes(self, session_id, revision_id, override):
        snapshot, _ = self.services["workspace_settings"].resolve(
            session_id, run_override=override
        )
        with self.database.session() as session:
            context = AudioIdentityContext(session, snapshot)
            segment_ids = list(
                session.scalars(
                    select(GenerationSegment.id)
                    .where(GenerationSegment.plan_revision_id == revision_id)
                    .order_by(GenerationSegment.ordinal)
                )
            )
            expected = {
                segment.id: context.for_segment(segment)
                for segment in session.scalars(
                    select(GenerationSegment).where(
                        GenerationSegment.id.in_(segment_ids)
                    )
                )
            }
        for segment_id in segment_ids:
            artifact = self.services["artifacts"].register(
                self._wave_path(segment_id),
                kind="audio",
                role="generation_take",
                session_id=session_id,
                metadata={IDENTITY_KEY: expected[segment_id]},
            )
            with self.database.session() as session:
                segment = session.get(GenerationSegment, segment_id)
                segment.status = "completed"
                session.add(
                    AudioTake(
                        generation_segment_id=segment_id,
                        artifact_id=artifact.id,
                        status="completed",
                        is_active=True,
                        duration_ms=100,
                    )
                )
        return segment_ids

    def _statement_log(self):
        log = []

        def _record(conn, cursor, statement, parameters, context, executemany):
            log.append(statement)

        event.listen(self.database.engine, "before_cursor_execute", _record)
        self.addCleanup(
            event.remove, self.database.engine, "before_cursor_execute", _record
        )
        return log

    @staticmethod
    def _override(**tts):
        return {
            "tts": {
                "service": "openai",
                "model": "model-a",
                "voice": "voice-a",
                "language": "en",
                "tts_batch_size": 1,
                **tts,
            }
        }

    def test_revision_history_has_no_per_segment_queries_with_casting(self):
        from pandrator.web.generation_controls import save_generation_controls

        session_id = self._create_session()
        first = self._plan(session_id, 40)
        with self.database.session() as session:
            save_generation_controls(
                session,
                session_id,
                expected_revision=0,
                characters=[],
                cast={"narrator": {"voice": "voice-a"}},
            )
        self._seed_reusable_takes(session_id, first["active_revision_id"], self._override())
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/tts", headers=self.headers
        ).get_json()
        changed = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {**current["effective"], "casting_enabled": True}},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, changed.status_code, changed.get_json())

        log = self._statement_log()
        result = revision_history(self.database, session_id, limit=10)
        segment_selects = [item for item in log if "FROM generation_segments" in item]
        take_selects = [item for item in log if "FROM audio_takes" in item]
        artifact_selects = [item for item in log if "FROM artifacts" in item]
        # Statement budget scales with visible revisions (per-revision freeze
        # reads), never with segments: 40 segments must not add ~40 queries.
        revisions = len(result["items"])
        self.assertGreater(revisions, 0)
        self.assertLessEqual(
            len(segment_selects),
            6 + 3 * revisions,
            f"{len(segment_selects)} generation_segments SELECTs for "
            f"{revisions} revisions in {len(log)} statements",
        )
        self.assertLessEqual(len(take_selects), 4)
        # Settings resolution issues one artifact lookup per role (~14);
        # the identity loop itself adds a single IN query.
        self.assertLessEqual(len(artifact_selects), 20)

    def test_catalogue_builds_are_request_scoped_and_bounded(self):
        from pandrator.logic import tts_handler

        session_id = self._create_session()
        plan = self._plan(session_id, 60)
        self._seed_reusable_takes(session_id, plan["active_revision_id"], self._override())
        with patch.object(
            tts_handler, "_default_service_configs", wraps=tts_handler._default_service_configs
        ) as builds:
            revision_history(self.database, session_id, limit=10)
            first_call = builds.call_count
            revision_history(self.database, session_id, limit=10)
            second_call = builds.call_count - first_call
        # Bounded by distinct catalogue inputs (3 voice combos share one
        # snapshot catalogue), not by the 60 segments.
        self.assertLessEqual(first_call, 3, f"catalogue rebuilt {first_call}x")
        # A second identical read rebuilds again: no cross-request global.
        self.assertGreaterEqual(second_call, 1)

    def test_identity_split_matches_join_row_set_on_adversarial_takes(self):
        session_id = self._create_session()
        plan = self._plan(session_id, 4, voices=("v-a",))
        revision_id = plan["active_revision_id"]
        segment_ids = self._seed_reusable_takes(
            session_id, revision_id, self._override()
        )
        # revision_history inspects current session settings, so persist the
        # override the takes were seeded with before counting reuse.
        current = self.client.get(
            f"/api/v1/sessions/{session_id}/settings/tts", headers=self.headers
        ).get_json()
        changed = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {**current["effective"], **self._override()["tts"]}},
            headers={**self.headers, "If-Match": f'"{current["revision"]}"'},
        )
        self.assertEqual(200, changed.status_code, changed.get_json())
        with self.database.session() as session:
            # Inactive duplicate take: inner join row set ignores it.
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[1],
                    artifact_id=None,
                    status="completed",
                    is_active=False,
                    duration_ms=50,
                )
            )
            # Take whose artifact is deleted: excluded like the join filter.
            doomed = self.services["artifacts"].register(
                self._wave_path("doomed"),
                kind="audio",
                role="generation_take",
                session_id=session_id,
            )
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[2],
                    artifact_id=doomed.id,
                    status="completed",
                    is_active=True,
                    duration_ms=100,
                )
            )
            session.flush()
            session.get(Artifact, doomed.id).state = "deleted"
            # Removed segment with a completed take: totals keep it, the
            # identity loop skips it exactly like the removed filter.
            removed = session.get(GenerationSegment, segment_ids[3])
            removed.removed = True
        result = revision_history(self.database, session_id, limit=10)
        item = next(row for row in result["items"] if row["id"] == revision_id)
        self.assertEqual(4, item["segment_count"])
        self.assertEqual(3, item["active_segment_count"])
        self.assertEqual(3, item["reusable_segment_count"])
        self.assertEqual(0, item["stale_segment_count"])
        self.assertEqual(0, item["audio_identity_unknown_segment_count"])

    def test_generation_progress_counts_use_revision_removed_status_covering_index(self):
        session_id = self._create_session()
        plan = self._plan(session_id, 1)
        revision_id = plan["active_revision_id"]
        other_session_id = self._create_session("Other progress plan")
        other_plan = self._plan(other_session_id, 1)

        with self.database.session() as session:
            current_segment = session.scalar(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id == revision_id
                )
            )
            current_segment.status = "ready"
            session.add_all(
                [
                    GenerationSegment(
                        plan_revision_id=revision_id,
                        ordinal=2,
                        text="Included completed segment",
                        status="completed",
                    ),
                    GenerationSegment(
                        plan_revision_id=revision_id,
                        ordinal=3,
                        text="Removed completed segment",
                        status="completed",
                        removed=True,
                    ),
                    GenerationSegment(
                        plan_revision_id=revision_id,
                        ordinal=4,
                        text="Removed ready segment",
                        status="ready",
                        removed=True,
                    ),
                ]
            )
            other_segment = session.scalar(
                select(GenerationSegment).where(
                    GenerationSegment.plan_revision_id
                    == other_plan["active_revision_id"]
                )
            )
            other_segment.status = "completed"

        with self.database.session() as session:
            total = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == revision_id,
                    GenerationSegment.removed.is_(False),
                )
            )
            completed = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == revision_id,
                    GenerationSegment.removed.is_(False),
                    GenerationSegment.status == "completed",
                )
            )
            ready = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == revision_id,
                    GenerationSegment.removed.is_(False),
                    GenerationSegment.status == "ready",
                )
            )
        self.assertEqual(2, total)
        self.assertEqual(1, completed)
        self.assertEqual(1, ready)

        with self.database.engine.connect() as connection:
            self.assertIsNone(
                connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'sqlite_stat1'"
                ).scalar_one_or_none()
            )
            query_plan = connection.exec_driver_sql(
                "EXPLAIN QUERY PLAN SELECT count(*) FROM generation_segments "
                "WHERE plan_revision_id = ? AND removed IS 0 AND status = ?",
                (revision_id, "completed"),
            ).all()
        details = [str(row[3]).upper() for row in query_plan]
        self.assertTrue(
            any(
                "USING COVERING INDEX IX_GENERATION_SEGMENTS_REVISION_REMOVED_STATUS"
                in detail
                for detail in details
            ),
            details,
        )

    def test_repair_state_hash_is_deterministic(self):
        from pandrator.web import repair_batches as batches

        session_id = self._create_session()
        plan = self._plan(session_id, 6)
        self._seed_reusable_takes(session_id, plan["active_revision_id"], self._override())
        with self.database.session() as session:
            first = batches.repair_state_hash(session, plan["active_revision_id"])
        with self.database.session() as session:
            second = batches.repair_state_hash(session, plan["active_revision_id"])
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
