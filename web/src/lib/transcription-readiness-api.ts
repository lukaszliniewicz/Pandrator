import { apiJson } from './api';

type AssetStatus =
  'cached' | 'missing' | 'unresolved' | 'not_required' | 'will_download';

export type TranscriptionReadiness = {
  scope: 'local_model_files';
  status: AssetStatus;
  ready: boolean | null;
  engine: string;
  runtime_check_required: boolean;
  assets: {
    kind: string;
    id: string;
    status: AssetStatus;
    verification: string;
    /** Set on a missing model that only an Administrator's run may download. */
    download?: 'administrator_required';
  }[];
};

export function checkTranscriptionFiles(
  sessionId: string,
  settings: Record<string, unknown>,
  signal?: AbortSignal
) {
  return apiJson<TranscriptionReadiness>(
    `/api/v1/sessions/${encodeURIComponent(sessionId)}/transcription-readiness`,
    { method: 'POST', body: JSON.stringify({ settings }), signal }
  );
}
