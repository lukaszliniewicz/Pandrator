import { apiJson } from './api';

type AssetStatus = 'cached' | 'missing' | 'unresolved' | 'not_required';

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
