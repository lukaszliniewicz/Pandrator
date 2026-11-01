"""Enforce consistent execution and usage ownership roots."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "0079_execution_scope_consistency"
down_revision = "0078_library_scope_consistency"
branch_labels = None
depends_on = None


_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_execution_scope_jobs_insert",
        "jobs",
        """CREATE TRIGGER pandrator_execution_scope_jobs_insert
BEFORE INSERT ON jobs
BEGIN
    SELECT RAISE(ABORT, 'job workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'job project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'job session must match its project')
    WHERE NEW.session_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM sessions AS member
          JOIN projects AS project ON project.id = member.project_id
          WHERE member.id IS NEW.session_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'job workflow must match its project and session')
    WHERE NEW.workflow_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM workflow_runs AS workflow
          JOIN sessions AS member ON member.id = workflow.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE workflow.id IS NEW.workflow_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
            AND (NEW.session_id IS NULL OR workflow.session_id IS NEW.session_id)
      );
END""",
    ),
    (
        "pandrator_execution_scope_jobs_update",
        "jobs",
        """CREATE TRIGGER pandrator_execution_scope_jobs_update
BEFORE UPDATE OF workspace_id, project_id, session_id, workflow_run_id ON jobs
WHEN NEW.workspace_id IS NOT OLD.workspace_id
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.session_id IS NOT OLD.session_id
  OR NEW.workflow_run_id IS NOT OLD.workflow_run_id
BEGIN
    SELECT RAISE(ABORT, 'job execution roots are immutable');
END""",
    ),
    (
        "pandrator_execution_scope_workflow_runs_update",
        "workflow_runs",
        """CREATE TRIGGER pandrator_execution_scope_workflow_runs_update
BEFORE UPDATE OF session_id ON workflow_runs
WHEN NEW.session_id IS NOT OLD.session_id
BEGIN
    SELECT RAISE(ABORT, 'workflow session is immutable');
END""",
    ),
    (
        "pandrator_execution_scope_generation_runs_insert",
        "generation_runs",
        """CREATE TRIGGER pandrator_execution_scope_generation_runs_insert
BEFORE INSERT ON generation_runs
BEGIN
    SELECT RAISE(ABORT, 'generation job must share its session scope')
    WHERE NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM jobs AS job
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS project.workspace_id
            AND (job.project_id IS NULL OR job.project_id IS member.project_id)
      );
END""",
    ),
    (
        "pandrator_execution_scope_generation_runs_update",
        "generation_runs",
        """CREATE TRIGGER pandrator_execution_scope_generation_runs_update
BEFORE UPDATE OF session_id, job_id ON generation_runs
BEGIN
    SELECT RAISE(ABORT, 'generation session is immutable')
    WHERE NEW.session_id IS NOT OLD.session_id;

    SELECT RAISE(ABORT, 'generation job must share its session scope')
    WHERE NEW.job_id IS NOT OLD.job_id
      AND NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM jobs AS job
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS project.workspace_id
            AND (job.project_id IS NULL OR job.project_id IS member.project_id)
      );
END""",
    ),
    (
        "pandrator_execution_scope_agent_runs_insert",
        "agent_runs",
        """CREATE TRIGGER pandrator_execution_scope_agent_runs_insert
BEFORE INSERT ON agent_runs
BEGIN
    SELECT RAISE(ABORT, 'agent job must share its session scope')
    WHERE NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM jobs AS job
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS project.workspace_id
            AND (job.project_id IS NULL OR job.project_id IS member.project_id)
      );

    SELECT RAISE(ABORT, 'agent source artifact must share its session workspace')
    WHERE NEW.source_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM artifacts AS artifact
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE artifact.id IS NEW.source_artifact_id
            AND artifact.workspace_id IS project.workspace_id
      );

    SELECT RAISE(ABORT, 'agent result artifact must share its session scope')
    WHERE NEW.result_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM artifacts AS artifact
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE artifact.id IS NEW.result_artifact_id
            AND artifact.workspace_id IS project.workspace_id
            AND (artifact.project_id IS NULL OR artifact.project_id IS member.project_id)
      );
END""",
    ),
    (
        "pandrator_execution_scope_agent_runs_update",
        "agent_runs",
        """CREATE TRIGGER pandrator_execution_scope_agent_runs_update
BEFORE UPDATE OF session_id, job_id, source_artifact_id, result_artifact_id ON agent_runs
BEGIN
    SELECT RAISE(ABORT, 'agent session is immutable')
    WHERE NEW.session_id IS NOT OLD.session_id;

    SELECT RAISE(ABORT, 'agent job must share its session scope')
    WHERE NEW.job_id IS NOT OLD.job_id
      AND NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM jobs AS job
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS project.workspace_id
            AND (job.project_id IS NULL OR job.project_id IS member.project_id)
      );

    SELECT RAISE(ABORT, 'agent source artifact must share its session workspace')
    WHERE NEW.source_artifact_id IS NOT OLD.source_artifact_id
      AND NEW.source_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM artifacts AS artifact
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE artifact.id IS NEW.source_artifact_id
            AND artifact.workspace_id IS project.workspace_id
      );

    SELECT RAISE(ABORT, 'agent result artifact must share its session scope')
    WHERE NEW.result_artifact_id IS NOT OLD.result_artifact_id
      AND NEW.result_artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM artifacts AS artifact
          JOIN sessions AS member ON member.id IS NEW.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE artifact.id IS NEW.result_artifact_id
            AND artifact.workspace_id IS project.workspace_id
            AND (artifact.project_id IS NULL OR artifact.project_id IS member.project_id)
      );
END""",
    ),
    (
        "pandrator_execution_scope_usage_events_insert",
        "usage_events",
        """CREATE TRIGGER pandrator_execution_scope_usage_events_insert
BEFORE INSERT ON usage_events
BEGIN
    SELECT RAISE(ABORT, 'usage workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'usage project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage session must match its stored scope')
    WHERE NEW.session_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM sessions AS member
          JOIN projects AS project ON project.id = member.project_id
          WHERE member.id IS NEW.session_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage workflow must match its stored scope')
    WHERE NEW.workflow_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM workflow_runs AS workflow
          JOIN sessions AS member ON member.id = workflow.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE workflow.id IS NEW.workflow_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage generation must match its stored scope')
    WHERE NEW.generation_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM generation_runs AS generation
          JOIN sessions AS member ON member.id = generation.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE generation.id IS NEW.generation_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage agent must match its stored scope')
    WHERE NEW.agent_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM agent_runs AS agent
          JOIN sessions AS member ON member.id = agent.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE agent.id IS NEW.agent_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage job must match its stored scope')
    WHERE NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS job
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS NEW.workspace_id
            AND (job.project_id IS NULL OR job.project_id IS NEW.project_id)
      );

    SELECT RAISE(ABORT, 'usage artifact must match its stored scope')
    WHERE NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
            AND (artifact.project_id IS NULL OR artifact.project_id IS NEW.project_id)
      );
END""",
    ),
    (
        "pandrator_execution_scope_usage_events_update",
        "usage_events",
        """CREATE TRIGGER pandrator_execution_scope_usage_events_update
BEFORE UPDATE OF workspace_id, project_id, session_id, workflow_run_id, job_id,
    artifact_id, generation_run_id, agent_run_id ON usage_events
BEGIN
    SELECT RAISE(ABORT, 'usage scope is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id
       OR NEW.project_id IS NOT OLD.project_id;

    SELECT RAISE(ABORT, 'usage session must match its stored scope')
    WHERE NEW.session_id IS NOT OLD.session_id
      AND NEW.session_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM sessions AS member
          JOIN projects AS project ON project.id = member.project_id
          WHERE member.id IS NEW.session_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage workflow must match its stored scope')
    WHERE NEW.workflow_run_id IS NOT OLD.workflow_run_id
      AND NEW.workflow_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM workflow_runs AS workflow
          JOIN sessions AS member ON member.id = workflow.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE workflow.id IS NEW.workflow_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage generation must match its stored scope')
    WHERE NEW.generation_run_id IS NOT OLD.generation_run_id
      AND NEW.generation_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM generation_runs AS generation
          JOIN sessions AS member ON member.id = generation.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE generation.id IS NEW.generation_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage agent must match its stored scope')
    WHERE NEW.agent_run_id IS NOT OLD.agent_run_id
      AND NEW.agent_run_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1
          FROM agent_runs AS agent
          JOIN sessions AS member ON member.id = agent.session_id
          JOIN projects AS project ON project.id = member.project_id
          WHERE agent.id IS NEW.agent_run_id
            AND member.project_id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'usage job must match its stored scope')
    WHERE NEW.job_id IS NOT OLD.job_id
      AND NEW.job_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM jobs AS job
          WHERE job.id IS NEW.job_id
            AND job.workspace_id IS NEW.workspace_id
            AND (job.project_id IS NULL OR job.project_id IS NEW.project_id)
      );

    SELECT RAISE(ABORT, 'usage artifact must match its stored scope')
    WHERE NEW.artifact_id IS NOT OLD.artifact_id
      AND NEW.artifact_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM artifacts AS artifact
          WHERE artifact.id IS NEW.artifact_id
            AND artifact.workspace_id IS NEW.workspace_id
            AND (artifact.project_id IS NULL OR artifact.project_id IS NEW.project_id)
      );
END""",
    ),
)


_REQUIRED_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("workspaces", ("id",)),
    ("projects", ("id", "workspace_id")),
    ("sessions", ("id", "project_id")),
    ("workflow_runs", ("id", "session_id")),
    ("jobs", ("id", "workspace_id", "project_id", "session_id", "workflow_run_id")),
    ("artifacts", ("id", "workspace_id", "project_id")),
    ("generation_runs", ("id", "session_id", "job_id")),
    (
        "agent_runs",
        ("id", "session_id", "job_id", "source_artifact_id", "result_artifact_id"),
    ),
    (
        "usage_events",
        (
            "id",
            "workspace_id",
            "project_id",
            "session_id",
            "workflow_run_id",
            "job_id",
            "artifact_id",
            "generation_run_id",
            "agent_run_id",
        ),
    ),
)

_REQUIRED_FOREIGN_KEYS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("projects", "workspace_id", "workspaces", "RESTRICT", False),
    ("sessions", "project_id", "projects", "RESTRICT", False),
    ("workflow_runs", "session_id", "sessions", "CASCADE", False),
    ("jobs", "workspace_id", "workspaces", "RESTRICT", False),
    ("jobs", "project_id", "projects", "RESTRICT", True),
    ("jobs", "session_id", "sessions", "CASCADE", True),
    ("jobs", "workflow_run_id", "workflow_runs", "CASCADE", True),
    ("artifacts", "workspace_id", "workspaces", "RESTRICT", False),
    ("artifacts", "project_id", "projects", "SET NULL", True),
    ("generation_runs", "session_id", "sessions", "CASCADE", False),
    ("generation_runs", "job_id", "jobs", "SET NULL", True),
    ("agent_runs", "session_id", "sessions", "CASCADE", False),
    ("agent_runs", "job_id", "jobs", "SET NULL", True),
    ("agent_runs", "source_artifact_id", "artifacts", "SET NULL", True),
    ("agent_runs", "result_artifact_id", "artifacts", "SET NULL", True),
    ("usage_events", "workspace_id", "workspaces", "RESTRICT", False),
    ("usage_events", "project_id", "projects", "RESTRICT", True),
    ("usage_events", "session_id", "sessions", "SET NULL", True),
    ("usage_events", "workflow_run_id", "workflow_runs", "SET NULL", True),
    ("usage_events", "job_id", "jobs", "SET NULL", True),
    ("usage_events", "artifact_id", "artifacts", "SET NULL", True),
    ("usage_events", "generation_run_id", "generation_runs", "SET NULL", True),
    ("usage_events", "agent_run_id", "agent_runs", "SET NULL", True),
)

_PREFLIGHT_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "projects",
        """SELECT project.id
FROM projects AS project
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
WHERE workspace.id IS NULL
ORDER BY project.id LIMIT 1""",
    ),
    (
        "sessions",
        """SELECT member.id
FROM sessions AS member
LEFT JOIN projects AS project ON project.id = member.project_id
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
WHERE project.id IS NULL OR workspace.id IS NULL
ORDER BY member.id LIMIT 1""",
    ),
    (
        "jobs",
        """SELECT job.id
FROM jobs AS job
LEFT JOIN workspaces AS workspace ON workspace.id = job.workspace_id
LEFT JOIN projects AS project ON project.id = job.project_id
LEFT JOIN sessions AS direct_session ON direct_session.id = job.session_id
LEFT JOIN projects AS direct_project ON direct_project.id = direct_session.project_id
LEFT JOIN workflow_runs AS workflow ON workflow.id = job.workflow_run_id
LEFT JOIN sessions AS workflow_session ON workflow_session.id = workflow.session_id
LEFT JOIN projects AS workflow_project ON workflow_project.id = workflow_session.project_id
WHERE workspace.id IS NULL
   OR (job.project_id IS NOT NULL AND (
       project.id IS NULL OR project.workspace_id IS NOT job.workspace_id
   ))
   OR (job.session_id IS NOT NULL AND (
       direct_session.id IS NULL
       OR direct_session.project_id IS NOT job.project_id
       OR direct_project.id IS NULL
       OR direct_project.workspace_id IS NOT job.workspace_id
   ))
   OR (job.workflow_run_id IS NOT NULL AND (
       workflow.id IS NULL
       OR workflow_session.id IS NULL
       OR workflow_session.project_id IS NOT job.project_id
       OR workflow_project.id IS NULL
       OR workflow_project.workspace_id IS NOT job.workspace_id
       OR (job.session_id IS NOT NULL AND workflow.session_id IS NOT job.session_id)
   ))
ORDER BY job.id LIMIT 1""",
    ),
    (
        "workflow_runs",
        """SELECT workflow.id
FROM workflow_runs AS workflow
LEFT JOIN sessions AS member ON member.id = workflow.session_id
LEFT JOIN projects AS project ON project.id = member.project_id
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
WHERE member.id IS NULL OR project.id IS NULL OR workspace.id IS NULL
ORDER BY workflow.id LIMIT 1""",
    ),
    (
        "generation_runs",
        """SELECT generation.id
FROM generation_runs AS generation
LEFT JOIN sessions AS member ON member.id = generation.session_id
LEFT JOIN projects AS project ON project.id = member.project_id
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
LEFT JOIN jobs AS job ON job.id = generation.job_id
WHERE member.id IS NULL
   OR project.id IS NULL
   OR workspace.id IS NULL
   OR (generation.job_id IS NOT NULL AND (
       job.id IS NULL
       OR job.workspace_id IS NOT workspace.id
       OR (job.project_id IS NOT NULL AND job.project_id IS NOT member.project_id)
   ))
ORDER BY generation.id LIMIT 1""",
    ),
    (
        "agent_runs",
        """SELECT agent.id
FROM agent_runs AS agent
LEFT JOIN sessions AS member ON member.id = agent.session_id
LEFT JOIN projects AS project ON project.id = member.project_id
LEFT JOIN workspaces AS workspace ON workspace.id = project.workspace_id
LEFT JOIN jobs AS job ON job.id = agent.job_id
LEFT JOIN artifacts AS source ON source.id = agent.source_artifact_id
LEFT JOIN artifacts AS result ON result.id = agent.result_artifact_id
WHERE member.id IS NULL
   OR project.id IS NULL
   OR workspace.id IS NULL
   OR (agent.job_id IS NOT NULL AND (
       job.id IS NULL
       OR job.workspace_id IS NOT workspace.id
       OR (job.project_id IS NOT NULL AND job.project_id IS NOT member.project_id)
   ))
   OR (agent.source_artifact_id IS NOT NULL AND (
       source.id IS NULL OR source.workspace_id IS NOT workspace.id
   ))
   OR (agent.result_artifact_id IS NOT NULL AND (
       result.id IS NULL
       OR result.workspace_id IS NOT workspace.id
       OR (result.project_id IS NOT NULL AND result.project_id IS NOT member.project_id)
   ))
ORDER BY agent.id LIMIT 1""",
    ),
    (
        "usage_events",
        """SELECT receipt.id
FROM usage_events AS receipt
LEFT JOIN workspaces AS workspace ON workspace.id = receipt.workspace_id
LEFT JOIN projects AS receipt_project ON receipt_project.id = receipt.project_id
LEFT JOIN sessions AS direct_session ON direct_session.id = receipt.session_id
LEFT JOIN projects AS direct_project ON direct_project.id = direct_session.project_id
LEFT JOIN workflow_runs AS workflow ON workflow.id = receipt.workflow_run_id
LEFT JOIN sessions AS workflow_session ON workflow_session.id = workflow.session_id
LEFT JOIN projects AS workflow_project ON workflow_project.id = workflow_session.project_id
LEFT JOIN generation_runs AS generation ON generation.id = receipt.generation_run_id
LEFT JOIN sessions AS generation_session ON generation_session.id = generation.session_id
LEFT JOIN projects AS generation_project ON generation_project.id = generation_session.project_id
LEFT JOIN agent_runs AS agent ON agent.id = receipt.agent_run_id
LEFT JOIN sessions AS agent_session ON agent_session.id = agent.session_id
LEFT JOIN projects AS agent_project ON agent_project.id = agent_session.project_id
LEFT JOIN jobs AS job ON job.id = receipt.job_id
LEFT JOIN artifacts AS artifact ON artifact.id = receipt.artifact_id
WHERE workspace.id IS NULL
   OR (receipt.project_id IS NOT NULL AND (
       receipt_project.id IS NULL OR receipt_project.workspace_id IS NOT receipt.workspace_id
   ))
   OR (receipt.session_id IS NOT NULL AND (
       direct_session.id IS NULL
       OR direct_project.id IS NULL
       OR direct_session.project_id IS NOT receipt.project_id
       OR direct_project.workspace_id IS NOT receipt.workspace_id
   ))
   OR (receipt.workflow_run_id IS NOT NULL AND (
       workflow.id IS NULL
       OR workflow_session.id IS NULL
       OR workflow_project.id IS NULL
       OR workflow_session.project_id IS NOT receipt.project_id
       OR workflow_project.workspace_id IS NOT receipt.workspace_id
   ))
   OR (receipt.generation_run_id IS NOT NULL AND (
       generation.id IS NULL
       OR generation_session.id IS NULL
       OR generation_project.id IS NULL
       OR generation_session.project_id IS NOT receipt.project_id
       OR generation_project.workspace_id IS NOT receipt.workspace_id
   ))
   OR (receipt.agent_run_id IS NOT NULL AND (
       agent.id IS NULL
       OR agent_session.id IS NULL
       OR agent_project.id IS NULL
       OR agent_session.project_id IS NOT receipt.project_id
       OR agent_project.workspace_id IS NOT receipt.workspace_id
   ))
   OR (receipt.job_id IS NOT NULL AND (
       job.id IS NULL
       OR job.workspace_id IS NOT receipt.workspace_id
       OR (job.project_id IS NOT NULL AND job.project_id IS NOT receipt.project_id)
   ))
   OR (receipt.artifact_id IS NOT NULL AND (
       artifact.id IS NULL
       OR artifact.workspace_id IS NOT receipt.workspace_id
       OR (artifact.project_id IS NOT NULL AND artifact.project_id IS NOT receipt.project_id)
   ))
ORDER BY receipt.id LIMIT 1""",
    ),
)

_EXPECTED_PROJECT_UPDATE_SQL = """CREATE TRIGGER pandrator_project_graph_projects_update
BEFORE UPDATE OF workspace_id, source_session_id ON projects
BEGIN
    SELECT RAISE(ABORT, 'project workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'project source must belong to project')
    WHERE NEW.source_session_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM sessions AS source
          WHERE source.id = NEW.source_session_id
            AND source.project_id = NEW.id
      );

    SELECT RAISE(ABORT, 'project source is immutable after attachment')
    WHERE OLD.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NOT OLD.source_session_id;

    SELECT RAISE(ABORT, 'project source cannot be cleared while its session exists')
    WHERE OLD.source_session_id IS NOT NULL
      AND NEW.source_session_id IS NULL
      AND EXISTS (
          SELECT 1 FROM sessions AS source
          WHERE source.id = OLD.source_session_id
      );
END"""

_EXPECTED_SESSION_UPDATE_SQL = """CREATE TRIGGER pandrator_project_graph_sessions_update
BEFORE UPDATE OF project_id ON sessions
WHEN NEW.project_id IS NOT OLD.project_id
BEGIN
    SELECT RAISE(ABORT, 'session project is immutable');
END"""

_EXPECTED_ARTIFACT_UPDATE_SQL = """CREATE TRIGGER pandrator_media_scope_artifacts_update
BEFORE UPDATE OF workspace_id, project_id, session_id ON artifacts
BEGIN
    SELECT RAISE(ABORT, 'artifact workspace is immutable')
    WHERE NEW.workspace_id IS NOT OLD.workspace_id;

    SELECT RAISE(ABORT, 'artifact project is immutable')
    WHERE NEW.project_id IS NOT OLD.project_id
      AND NOT (
          OLD.project_id IS NOT NULL
          AND NEW.project_id IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM projects AS project
              WHERE project.id IS OLD.project_id
          )
      );

    SELECT RAISE(ABORT, 'artifact workspace does not exist')
    WHERE NOT EXISTS (
        SELECT 1 FROM workspaces AS workspace
        WHERE workspace.id IS NEW.workspace_id
    );

    SELECT RAISE(ABORT, 'artifact project must share its workspace')
    WHERE NEW.project_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM projects AS project
          WHERE project.id IS NEW.project_id
            AND project.workspace_id IS NEW.workspace_id
      );

    SELECT RAISE(ABORT, 'artifact session must belong to its project')
    WHERE NEW.session_id IS NOT NULL
      AND (
          NEW.project_id IS NULL
          OR NOT EXISTS (
              SELECT 1
              FROM sessions AS member
              JOIN projects AS project ON project.id = member.project_id
              WHERE member.id IS NEW.session_id
                AND member.project_id IS NEW.project_id
                AND project.workspace_id IS NEW.workspace_id
          )
      );
END"""

_EXPECTED_JOB_WORKSPACE_UPDATE_SQL = """CREATE TRIGGER pandrator_library_scope_jobs_update
BEFORE UPDATE OF workspace_id ON jobs
WHEN NEW.workspace_id IS NOT OLD.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'job workspace is immutable');
END"""

_REQUIRED_PARENT_TRIGGERS: tuple[tuple[str, str, str], ...] = (
    (
        "pandrator_project_graph_projects_update",
        "projects",
        _EXPECTED_PROJECT_UPDATE_SQL,
    ),
    (
        "pandrator_project_graph_sessions_update",
        "sessions",
        _EXPECTED_SESSION_UPDATE_SQL,
    ),
    (
        "pandrator_media_scope_artifacts_update",
        "artifacts",
        _EXPECTED_ARTIFACT_UPDATE_SQL,
    ),
    (
        "pandrator_library_scope_jobs_update",
        "jobs",
        _EXPECTED_JOB_WORKSPACE_UPDATE_SQL,
    ),
)


def _columns(bind: sa.Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in sa.inspect(bind).get_columns(table_name)}


def _has_expected_foreign_key(
    bind: sa.Connection,
    table_name: str,
    column_name: str,
    parent_table: str,
    ondelete: str,
) -> bool:
    matches = [
        foreign_key
        for foreign_key in sa.inspect(bind).get_foreign_keys(table_name)
        if column_name in (foreign_key.get("constrained_columns") or ())
    ]
    if len(matches) != 1:
        return False
    foreign_key = matches[0]
    options = foreign_key.get("options") or {}
    return (
        tuple(foreign_key.get("constrained_columns") or ()) == (column_name,)
        and foreign_key.get("referred_table") == parent_table
        and tuple(foreign_key.get("referred_columns") or ()) == ("id",)
        and str(options.get("ondelete") or "").upper() == ondelete
    )


def _validate_schema(bind: sa.Connection) -> None:
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    for table_name, required_columns in _REQUIRED_COLUMNS:
        if table_name not in tables:
            raise RuntimeError(
                "Cannot enforce execution scope consistency; a required table is missing."
            )
        columns = _columns(bind, table_name)
        if any(column_name not in columns for column_name in required_columns):
            raise RuntimeError(
                "Cannot enforce execution scope consistency; a required column is missing."
            )

    for table_name, column_name, parent_table, ondelete, nullable in _REQUIRED_FOREIGN_KEYS:
        column = _columns(bind, table_name)[column_name]
        column_type = column["type"]
        if (
            not isinstance(column_type, sa.String)
            or getattr(column_type, "length", None) != 36
            or bool(column.get("nullable")) != nullable
            or column.get("default") is not None
            or not _has_expected_foreign_key(bind, table_name, column_name, parent_table, ondelete)
        ):
            raise RuntimeError(
                "Cannot enforce execution scope consistency; an execution ownership foreign key has an unexpected shape."
            )


def _validate_parent_triggers(bind: sa.Connection) -> None:
    for name, expected_table, expected_sql in _REQUIRED_PARENT_TRIGGERS:
        row = bind.execute(
            sa.text(
                "SELECT name, tbl_name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND name = :name COLLATE NOCASE LIMIT 1"
            ),
            {"name": name},
        ).first()
        if (
            row is None
            or str(row[0]) != name
            or str(row[1]) != expected_table
            or str(row[2]) != expected_sql
        ):
            raise RuntimeError(
                "Cannot enforce execution scope consistency; a required parent trigger is missing or altered."
            )


def _validate_data(bind: sa.Connection) -> None:
    for table_name, query in _PREFLIGHT_QUERIES:
        row = bind.exec_driver_sql(query).first()
        if row is not None:
            raise RuntimeError(
                "Cannot enforce execution scope consistency; "
                f"{table_name} row {row[0]} has inconsistent stored roots."
            )


def _reject_name_collisions(bind: sa.Connection) -> None:
    for name, _table_name, _sql in _TRIGGERS:
        row = bind.execute(
            sa.text("SELECT 1 FROM sqlite_master WHERE name = :name COLLATE NOCASE LIMIT 1"),
            {"name": name},
        ).first()
        if row is not None:
            raise RuntimeError(
                "Cannot enforce execution scope consistency; an owned trigger name is already in use."
            )


def _sqlite_bind() -> sa.Connection:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("Execution scope consistency migration is supported only on SQLite.")
    return bind


def _expected_execution_trigger_sql(bind: sa.Connection) -> dict[str, tuple[str, str]]:
    names = tuple(name for name, _table_name, _sql in _TRIGGERS)
    parameters = {f"name_{index}": name for index, name in enumerate(names)}
    placeholders = ", ".join(f":name_{index}" for index in range(len(names)))
    rows = bind.execute(
        sa.text(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            f"WHERE type = 'trigger' AND name IN ({placeholders}) ORDER BY name"
        ),
        parameters,
    ).all()
    actual = {str(name): (str(table_name), str(sql)) for name, table_name, sql in rows}
    expected = {name: (table_name, sql) for name, table_name, sql in _TRIGGERS}
    if actual != expected:
        raise RuntimeError(
            "Cannot downgrade execution scope consistency; an owned trigger is missing or altered."
        )
    return expected


def upgrade() -> None:
    bind = _sqlite_bind()
    _validate_schema(bind)
    _validate_parent_triggers(bind)
    _validate_data(bind)
    _reject_name_collisions(bind)
    for _name, _table_name, sql in _TRIGGERS:
        op.execute(sql)


def downgrade() -> None:
    bind = _sqlite_bind()
    _expected_execution_trigger_sql(bind)
    preparer = bind.dialect.identifier_preparer
    for name, _table_name, _sql in reversed(_TRIGGERS):
        op.execute(f"DROP TRIGGER {preparer.quote(name)}")
