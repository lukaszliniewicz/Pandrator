import { apiJson } from './api';
import { invalidationBus } from './invalidation';
import type { RepairBatch } from './repair-batches';

export type SessionSourceItem = {
  artifact_id: string;
  source_asset_id: string | null;
  attachment_id: string | null;
  filename: string;
  profile: string;
  size_bytes: number | null;
  content_hash: string | null;
};
export type SessionSourceState = {
  session_id: string;
  session_revision: number;
  primary: SessionSourceItem | null;
  media: SessionSourceItem | null;
  blocked_reason: string | null;
  cleanup_pending?: number;
  timing_review?: { required?: boolean; media_artifact_id?: string | null };
  subtitle: {
    supported: boolean;
    source_asset_id: string | null;
    adoption_required: boolean;
    cue_count: number;
    can_align: boolean;
    word_timing_artifact_id: string | null;
    alignment_note: string | null;
  };
};
export type SourceChangeImpact = {
  session_revision: number;
  impact_token: string;
  destructive: boolean;
  blocked_reason: string | null;
  counts: Record<string, number>;
  artifact_counts: Record<string, number>;
  derived_files: number;
  shared_files_retained: number;
};
export type SpeechPlanRevision = {
  id: string;
  entry_id?: string;
  is_repair_checkpoint?: boolean;
  repair_batch?: RepairBatch | null;
  revision_number: number;
  summary: string;
  origin: string;
  reviewed: boolean;
  compatible: boolean;
  segment_count: number;
  active_segment_count: number;
  reusable_segment_count: number | null;
  stale_segment_count: number | null;
  audio_reuse_checked?: boolean;
  audio_settings_stale_segment_count?: number | null;
  audio_identity_unknown_segment_count?: number | null;
  source_artifact_id: string | null;
};
export type SpeechPlanState = {
  session_id: string;
  session_revision: number;
  items: SpeechPlanRevision[];
  selected_revision_id: string | null;
  latest_revision_id: string | null;
  content_signature: string | null;
  can_prepare: boolean;
  can_generate: boolean;
  generation_blocked_reason?: string | null;
  current_input: {
    artifact_id: string;
    label: string;
    version?: number | null;
    role: string;
  } | null;
  blocked_reason: string | null;
  warning: string | null;
};

export function notifySessionFlowChange(sessionId: string) {
  invalidationBus.publish({
    resources: ['sources', 'sessions', 'workflow', 'generation', 'output'],
    session_ids: [sessionId],
    job_ids: [],
    events: [{ type: 'session.flow.changed', session_id: sessionId }]
  });
}

export async function sessionFlowAction<T = Record<string, unknown>>(
  sessionId: string,
  suffix: string,
  body: Record<string, unknown>,
  notify = true
): Promise<T> {
  const result = await apiJson<T>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/${suffix}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json'
      },
      body: JSON.stringify(body)
    }
  );
  if (notify) notifySessionFlowChange(sessionId);
  return result;
}

export const sourceState = (sessionId: string) =>
  apiJson<SessionSourceState>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/sources/status`
  );
// Summary mode skips server-side audio-reuse inspection for a fast list path;
// reuse counts arrive as null with audio_reuse_checked=false and must never be
// rendered as 0. Default (no options) preserves the legacy full payload.
export const speechPlanState = (
  sessionId: string,
  options?: { summary?: boolean }
) =>
  apiJson<SpeechPlanState>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/generation-plan/status${options?.summary ? '?summary=true' : ''}`
  );

export type SpeechPlanHistoryPage = {
  items: SpeechPlanHistoryItem[];
  active_revision_id: string | null;
  next_before_revision_number: number | null;
};

// Grouped-history rows: full revision_history fields plus grouping decorations.
// Optional fields stay optional so summary payloads (null reuse counts,
// unchecked guards) and full payloads share one type.
type SpeechPlanHistoryItem = SpeechPlanRevision & {
  parent_revision_id: string | null;
  source_revision_id?: string | null;
  action?: string;
  reason?: string | null;
  repair_status?: string | null;
  repair_reason?: string | null;
  source_generation_run_id?: string | null;
  source_block_ordinal?: number | null;
  restored_from_revision_id?: string | null;
  history_revision_number?: number;
  is_active?: boolean;
  created_at?: string;
};

export const speechPlanHistory = (
  sessionId: string,
  options?: {
    summary?: boolean;
    before_revision_number?: number | null;
    limit?: number;
  }
) => {
  const query = new URLSearchParams({
    limit: String(options?.limit ?? 50)
  });
  if (options?.summary) query.set('summary', 'true');
  if (options?.before_revision_number != null)
    query.set('before_revision_number', String(options.before_revision_number));
  return apiJson<SpeechPlanHistoryPage>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/generation-plan/history?${query}`
  );
};

// Authoritative on-demand detail for one selected revision (always full).
export const speechPlanRevision = (sessionId: string, revisionId: string) =>
  apiJson<{ revision: SpeechPlanRevision; active_revision_id: string | null }>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/generation-plan/revisions/${encodeURIComponent(revisionId)}`
  );

// UI commands are separate from data invalidation and scoped to mounted consumers.
const speechPlanEditorListeners = new Set<(sessionId: string) => void>();

export function subscribeSpeechPlanEditor(
  listener: (sessionId: string) => void
) {
  speechPlanEditorListeners.add(listener);
  return () => speechPlanEditorListeners.delete(listener);
}

export function openSpeechPlanEditor(sessionId: string) {
  for (const listener of speechPlanEditorListeners) listener(sessionId);
}
