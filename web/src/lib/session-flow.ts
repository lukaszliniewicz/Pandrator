import { apiJson } from './api';
import { invalidationBus } from './invalidation';

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
  revision_number: number;
  summary: string;
  origin: string;
  reviewed: boolean;
  compatible: boolean;
  segment_count: number;
  active_segment_count: number;
  reusable_segment_count: number;
  stale_segment_count: number;
  audio_settings_stale_segment_count?: number;
  audio_identity_unknown_segment_count?: number;
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
export const speechPlanState = (sessionId: string) =>
  apiJson<SpeechPlanState>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/generation-plan/status`
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
