import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from alembic import command
from alembic.config import Config

from pandrator.runtime import DataPaths
from pandrator.web.database import (
    SCHEMA_HEAD,
    Database,
    sqlite_url,
    upgrade_database,
)
from scripts.phase0_baseline import (
    benchmark_generation_assembly,
    benchmark_workflow_snapshot,
)


class WebDatabaseEfficiencyTests(unittest.TestCase):
    def measure_workflow(self, artifact_count: int, job_count: int) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            upgrade_database(paths.database)
            database = Database(paths.database)
            try:
                return benchmark_workflow_snapshot(
                    database,
                    artifact_count=artifact_count,
                    job_count=job_count,
                )
            finally:
                database.dispose()

    def measure_assembly(
        self,
        segment_count: int,
        *,
        run_scoped: bool = False,
    ) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            paths = DataPaths.from_value(directory).ensure()
            upgrade_database(paths.database)
            database = Database(paths.database)
            try:
                return benchmark_generation_assembly(
                    paths,
                    database,
                    segment_count=segment_count,
                    run_scoped=run_scoped,
                )
            finally:
                database.dispose()

    def test_workflow_query_and_load_counts_do_not_scale_with_history(self):
        small = self.measure_workflow(10, 10)
        large = self.measure_workflow(1000, 4000)

        self.assertEqual(small["select_count"], large["select_count"])
        # The stable workflow snapshot baseline is 13 queries; equality above
        # remains the scaling guard as history grows from 10 to 4,000 runs.
        self.assertLessEqual(large["select_count"], 13)
        self.assertLessEqual(large["orm_objects_loaded"], 50)
        self.assertLess(large["response_json_bytes"], 25_000)

    def test_assembly_select_count_does_not_scale_with_segments(self):
        small = self.measure_assembly(3)
        large = self.measure_assembly(100)

        self.assertEqual(small["select_count"], large["select_count"])
        self.assertLessEqual(large["select_count"], 8)
        self.assertLessEqual(large["selects_per_segment"], 0.08)

        small_run = self.measure_assembly(3, run_scoped=True)
        large_run = self.measure_assembly(100, run_scoped=True)
        self.assertEqual(
            small_run["select_count"],
            large_run["select_count"],
        )
        self.assertLessEqual(large_run["select_count"], 9)
        self.assertLessEqual(large_run["selects_per_segment"], 0.09)

    def test_representative_query_plans_use_covering_indexes_without_temp_sorts(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "plans.sqlite3"
            upgrade_database(database_path)
            queries = {
                "latest_jobs": (
                    """
                    SELECT id FROM (
                        SELECT id,
                               row_number() OVER (
                                   PARTITION BY kind
                                   ORDER BY created_at DESC, id DESC
                               ) AS rank
                        FROM jobs
                        WHERE session_id = ?
                          AND kind IN (
                              'source.clean',
                              'text.prepare',
                              'workflow.continue'
                          )
                    )
                    WHERE rank = 1
                    """,
                    ("session",),
                    "idx_jobs_session_kind_created",
                ),
                "artifact_history": (
                    """
                    SELECT id FROM (
                        SELECT id,
                               role,
                               row_number() OVER (
                                   PARTITION BY role
                                   ORDER BY created_at DESC, id DESC
                               ) AS rank
                        FROM artifacts
                        WHERE session_id = ?
                          AND role IN (
                              'clean_text',
                              'prepared_text',
                              'tts_optimized'
                          )
                    )
                    WHERE rank <= 10
                    """,
                    ("session",),
                    "idx_artifacts_session_role_created",
                ),
                "active_takes": (
                    """
                    SELECT id FROM (
                        SELECT audio_takes.id,
                               row_number() OVER (
                                   PARTITION BY generation_segment_id
                                   ORDER BY audio_takes.created_at DESC,
                                            audio_takes.id DESC
                               ) AS rank
                        FROM audio_takes
                        JOIN generation_segments
                          ON audio_takes.generation_segment_id =
                             generation_segments.id
                        WHERE generation_segments.plan_revision_id = ?
                          AND generation_segments.removed = 0
                          AND audio_takes.is_active = 1
                          AND audio_takes.status = 'completed'
                    )
                    WHERE rank = 1
                    """,
                    ("revision",),
                    "idx_audio_takes_active_status_segment_created",
                ),
                "job_claim": (
                    """
                    SELECT id
                    FROM jobs
                    WHERE status = 'queued'
                      AND attempts < max_attempts
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (),
                    "idx_jobs_status_created",
                ),
                "edge_parents": (
                    """
                    SELECT parent_artifact_id
                    FROM artifact_edges
                    WHERE child_artifact_id = ?
                    """,
                    ("artifact",),
                    "idx_artifact_edges_child_parent",
                ),
            }
            with closing(sqlite3.connect(database_path)) as connection:
                for name, (statement, parameters, index_name) in queries.items():
                    details = [
                        row[3]
                        for row in connection.execute(
                            f"EXPLAIN QUERY PLAN {statement}",
                            parameters,
                        )
                    ]
                    self.assertTrue(
                        any(index_name in detail for detail in details),
                        f"{name} did not use {index_name}: {details}",
                    )
                    self.assertFalse(
                        any("TEMP B-TREE" in detail for detail in details),
                        f"{name} used a temporary sort: {details}",
                    )

    def test_index_migration_completes_on_large_existing_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "migration.sqlite3"
            upgrade_database(database_path)
            config = Config()
            config.set_main_option(
                "script_location",
                str(
                    Path(__file__).parents[1]
                    / "pandrator"
                    / "web"
                    / "migrations"
                ),
            )
            config.set_main_option("sqlalchemy.url", sqlite_url(database_path))
            command.downgrade(config, "0020_capability_snapshots")

            count = 20_000
            timestamp = "2026-01-01 00:00:00"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.executemany(
                    """
                    INSERT INTO jobs (
                        id, kind, status, payload_json, resource_keys_json,
                        progress, lease_generation, attempts, max_attempts,
                        created_at, updated_at
                    )
                    VALUES (?, ?, 'succeeded', '{}', '[]', 1.0, 0, 1, 1, ?, ?)
                    """,
                    (
                        (
                            f"job-{index:08d}",
                            f"kind-{index % 8}",
                            timestamp,
                            timestamp,
                        )
                        for index in range(count)
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO artifacts (
                        id, kind, role, relative_path, state, metadata_json,
                        created_at, updated_at
                    )
                    VALUES (?, 'text', ?, ?, 'stale', '{}', ?, ?)
                    """,
                    (
                        (
                            f"artifact-{index:08d}",
                            f"role-{index % 8}",
                            f"migration/artifact-{index:08d}.txt",
                            timestamp,
                            timestamp,
                        )
                        for index in range(count)
                    ),
                )
                connection.commit()

            started = time.perf_counter()
            upgrade_database(database_path)
            elapsed = time.perf_counter() - started

            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    SCHEMA_HEAD,
                    connection.execute(
                        "SELECT version_num FROM alembic_version"
                    ).fetchone()[0],
                )
            self.assertLess(elapsed, 30.0)


if __name__ == "__main__":
    unittest.main()
