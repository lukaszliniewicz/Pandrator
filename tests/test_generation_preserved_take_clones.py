"""Missing-only and stale-only starts copy their preserved takes in bulk.

The write transaction used to load each preserved take, its segment and its
artifact one at a time and flush a deactivation and an insert per take: on a
5,167-segment book with 5,159 preserved takes that held the writer for 7.7 s. The
copies are now one chunked UPDATE and one bulk INSERT after the existing row-state
recheck. These tests compare the result with the old per-take loop, kept below as
the reference, and count statements, never time.
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from sqlalchemy import event, select

from pandrator.web import workspace
from pandrator.web.generation_audio_identity import take_reuse_reason
from pandrator.web.models import Artifact, AudioTake, GenerationSegment
from pandrator.web.workspace import RevisionConflict
from tests import test_generation_missing_only as missing_tests


def reference_clone(
    session, *, run_id, plan_revision_id, preserved_items, row_state, missing_only, identities,
) -> None:
    """The per-take loop ``start_in_session`` ran before the bulk copy."""
    del row_state, plan_revision_id
    for segment_id, take_id in preserved_items.items():
        source_take = session.get(AudioTake, take_id)
        segment = session.get(GenerationSegment, segment_id)
        artifact = (
            session.get(Artifact, source_take.artifact_id)
            if source_take and source_take.artifact_id else None
        )
        if missing_only:
            if (source_take is None or segment is None or artifact is None
                    or segment.status != "completed"
                    or source_take.generation_segment_id != segment_id
                    or source_take.status != "completed" or not source_take.is_active
                    or artifact.state == "deleted"):
                raise RevisionConflict("A preserved take changed.")
        elif (source_take is None or segment is None or artifact is None
                or segment.status != "completed"
                or source_take.generation_segment_id != segment_id
                or source_take.status != "completed" or not source_take.is_active
                or take_reuse_reason(segment, source_take, artifact, identities[segment_id])
                != "reusable"):
            raise RevisionConflict("A reusable take changed.")
        source_take.is_active = False
        session.add(AudioTake(
            generation_segment_id=segment_id,
            generation_run_id=run_id,
            artifact_id=source_take.artifact_id,
            parent_take_id=source_take.id,
            kind=source_take.kind,
            status="completed",
            settings_hash=source_take.settings_hash,
            duration_ms=source_take.duration_ms,
            is_active=True,
        ))
    session.flush()


class Statements:
    def __init__(self) -> None:
        self.items: list[str] = []

    def per_take_reads(self) -> int:
        return sum(
            1 for text in self.items
            if text.lstrip().upper().startswith("SELECT")
            and "FROM audio_takes" in text
            and "WHERE audio_takes.id = ?" in text
        )

    def writes(self, verb: str) -> int:
        return sum(
            1 for text in self.items
            if text.lstrip().upper().startswith(verb) and "audio_takes" in text.split("(")[0]
        )


class PreservedTakeCloneTests(unittest.TestCase):
    setUp = missing_tests.GenerationMissingOnlyTests.setUp
    _override = staticmethod(missing_tests.GenerationMissingOnlyTests._override)
    _create_case = missing_tests.GenerationMissingOnlyTests._create_case
    _resolved = missing_tests.GenerationMissingOnlyTests._resolved
    _wave_path = missing_tests.GenerationMissingOnlyTests._wave_path
    _seed_completed = missing_tests.GenerationMissingOnlyTests._seed_completed

    @contextmanager
    def _statements(self):
        recorded = Statements()

        def record(_connection, _cursor, statement, _parameters, _context, _many):
            recorded.items.append(statement)

        engine = self.database.engine
        event.listen(engine, "before_cursor_execute", record)
        try:
            yield recorded
        finally:
            event.remove(engine, "before_cursor_execute", record)

    def _case(self, preserved: int, missing: int) -> tuple[str, list[str]]:
        session_id, _revision, ids = self._create_case(
            texts=[f"Block number {index}." for index in range(preserved + missing)],
        )
        self._seed_completed(session_id, ids[:preserved], self._override())
        return session_id, ids

    def _takes(self, session_id: str, ids: list[str]) -> list[tuple[Any, ...]]:
        """Every take of the case, by segment position, without generated ids."""
        position = {segment_id: index for index, segment_id in enumerate(ids)}
        with self.database.session() as session:
            takes = list(session.scalars(
                select(AudioTake).where(AudioTake.generation_segment_id.in_(ids))
            ))
            by_id = {take.id: take for take in takes}
            return sorted(
                (
                    position[take.generation_segment_id],
                    take.generation_run_id is not None,
                    take.parent_take_id is not None
                    and by_id[take.parent_take_id].generation_run_id is None,
                    take.artifact_id,
                    take.kind,
                    take.status,
                    take.settings_hash,
                    take.duration_ms,
                    take.is_active,
                    take.revision,
                )
                for take in takes
            )

    def _vary_sources(self, ids: list[str]) -> None:
        """Give each preserved take its own kind, settings hash and duration."""
        position = {segment_id: index for index, segment_id in enumerate(ids)}
        with self.database.session() as session:
            for take in session.scalars(
                select(AudioTake).where(AudioTake.generation_segment_id.in_(ids))
            ):
                index = position[take.generation_segment_id]
                take.kind = "rvc" if index % 2 else "tts"
                take.settings_hash = f"settings-{index}"
                take.duration_ms = 1000 + index

    def _start(self, session_id: str, **mode: bool) -> dict[str, Any]:
        return self.generation.start(session_id, run_override=self._override(), **mode)

    def _compare_with_reference(self, **mode: bool) -> None:
        reference_id, reference_ids = self._case(preserved=5, missing=2)
        session_id, ids = self._case(preserved=5, missing=2)
        # Distinct source values, the same in both cases, so the copies must take
        # them over rather than match by accident.
        self._vary_sources(reference_ids)
        self._vary_sources(ids)
        with patch.object(workspace, "_clone_preserved_takes", reference_clone):
            self._start(reference_id, **mode)
        self._start(session_id, **mode)
        expected = self._takes(reference_id, reference_ids)
        actual = self._takes(session_id, ids)
        # Artifacts differ per case; compare everything else, then the artifact map.
        self.assertEqual(
            [row[:3] + row[4:] for row in expected], [row[:3] + row[4:] for row in actual],
        )
        self.assertEqual(10, len(actual))  # 5 sources and 5 active copies
        with self.database.session() as session:
            for segment_id in ids[:5]:
                takes = list(session.scalars(
                    select(AudioTake).where(AudioTake.generation_segment_id == segment_id)
                ))
                source = next(take for take in takes if take.generation_run_id is None)
                copy = next(take for take in takes if take.generation_run_id is not None)
                self.assertEqual(
                    (source.artifact_id, source.id, False, True),
                    (copy.artifact_id, copy.parent_take_id, source.is_active, copy.is_active),
                )

    def test_missing_only_copies_equal_the_per_take_loop(self):
        self._compare_with_reference(missing_only=True)

    def test_stale_only_copies_equal_the_per_take_loop(self):
        self._compare_with_reference(stale_only=True)

    def test_copies_below_the_chunk_size_cost_one_update_and_one_insert(self):
        counts = []
        for preserved in (3, 9):
            session_id, ids = self._case(preserved=preserved, missing=1)
            # Copies whose None columns differ still go in one batch.
            with self.database.session() as session:
                for take in session.scalars(
                    select(AudioTake).where(AudioTake.generation_segment_id.in_(ids[::2]))
                ):
                    take.settings_hash = "seeded-hash"
                    take.duration_ms = None
            with self._statements() as statements:
                self._start(session_id, missing_only=True)
            counts.append((
                statements.per_take_reads(),
                statements.writes("UPDATE"),
                statements.writes("INSERT"),
            ))
        # No per-take point read. The statement count is constant per 500 takes
        # (``_CLONE_CHUNK``): below it, one UPDATE deactivating the sources and one
        # INSERT of the copies, for 3 preserved takes as for 9.
        self.assertEqual([(0, 1, 1), (0, 1, 1)], counts)

    def test_no_copies_when_nothing_is_preserved(self):
        session_id, _ids = self._case(preserved=0, missing=3)
        with self._statements() as statements:
            self._start(session_id, missing_only=True)
        self.assertEqual((0, 0), (statements.writes("UPDATE"), statements.writes("INSERT")))

    def test_a_take_missing_from_the_rechecked_state_conflicts(self):
        with self.database.session() as session, self.assertRaises(RevisionConflict):
            workspace._clone_preserved_takes(
                session, run_id="run", plan_revision_id="revision",
                preserved_items={"segment": "take"},
                row_state={"segments": {}, "actives": {}, "artifacts": {}},
                missing_only=True, identities={},
            )
        with self.database.session() as session, self.assertRaises(RevisionConflict):
            workspace._clone_preserved_takes(
                session, run_id="run", plan_revision_id="revision",
                preserved_items={"segment": "take"},
                row_state=None, missing_only=True, identities={},
            )
