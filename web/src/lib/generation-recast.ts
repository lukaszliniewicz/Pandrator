import { apiJson } from './api';
import type { ApiMutationAttempt } from './api';
import type { GenerationRun } from './api-models';
import { notifySessionFlowChange } from './session-flow';

export type RecastSelector = {
  key: string;
  label: string;
  affected_count: number;
  changed_count: number;
  missing_count: number;
  unverified_count: number;
  overridden_count: number;
  error?: string | null;
};
export type RecastRequest = {
  selector_keys?: string[] | null;
  speech_plan_revision_id?: string | null;
  force?: boolean;
  include_missing?: boolean;
};
export type RecastPreview = {
  speech_plan_revision_id: string;
  controls_revision: number;
  selection_hash: string;
  settings_summary: { service: string; model: string; language?: string };
  selectors: RecastSelector[];
  selected_keys: string[];
  generate_count: number;
  keep_count: number;
  missing_count: number;
  shared_segment_count: number;
  blocked_reason?: string | null;
};
export type RecastUndoPreview = {
  generation_run_id: string;
  selection_hash: string;
  restore_count: number;
  blocked_reason?: string | null;
};
export function previewRecast(
  sessionId: string,
  body: RecastRequest = {},
  signal?: AbortSignal
) {
  return apiJson<RecastPreview>(
    `/sessions/${encodeURIComponent(sessionId)}/generation-recast/preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal
    }
  );
}
export async function startRecast(
  sessionId: string,
  body: RecastRequest & { expected_selection_hash: string },
  attempt: ApiMutationAttempt
) {
  const path = `/sessions/${encodeURIComponent(sessionId)}/generation-recast`;
  const run = await apiJson<GenerationRun>(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...attempt.headersFor(path, body)
    },
    body: JSON.stringify(body)
  });
  notifySessionFlowChange(sessionId);
  return run;
}
export function previewUndoRecast(runId: string) {
  return apiJson<RecastUndoPreview>(
    `/generation-runs/${encodeURIComponent(runId)}/audio-version/undo-preview`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }
  );
}
export async function undoRecast(
  runId: string,
  preview: RecastUndoPreview,
  attempt: ApiMutationAttempt
) {
  const path = `/generation-runs/${encodeURIComponent(runId)}/audio-version/undo`;
  const body = { expected_selection_hash: preview.selection_hash };
  const result = await apiJson<{ run: GenerationRun; restored_count: number }>(
    path,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...attempt.headersFor(path, body)
      },
      body: JSON.stringify(body)
    }
  );
  notifySessionFlowChange(result.run.session_id);
  return result;
}
