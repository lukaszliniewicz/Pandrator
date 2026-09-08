import { apiJson, apiJsonUpload } from './api';

export type TranscriptFormat = 'txt' | 'srt' | 'json';
export type QuickTranscription = {
  id: string;
  job_id: string | null;
  status: string;
  progress: number;
  progress_detail: string | null;
  expires_at: string;
  format: TranscriptFormat;
  result_available: boolean;
  inline_result: boolean;
  result_url: string;
  result?: {
    content: string | Record<string, unknown>;
    format: TranscriptFormat;
    size_bytes: number;
    mime_type: string;
  };
  error?: { code: string; message: string };
};

const path = (id: string) => `/api/v1/transcriptions/${encodeURIComponent(id)}`;

export const quickTranscriptionApi = {
  upload(
    file: File,
    options: Record<string, string>,
    key: string,
    onProgress: (percent: number) => void
  ) {
    const body = new FormData();
    body.set('file', file);
    body.set('options', JSON.stringify(options));
    return apiJsonUpload<QuickTranscription>(
      '/api/v1/transcriptions',
      {
        method: 'POST',
        body,
        headers: { 'Idempotency-Key': key }
      },
      (fraction) => onProgress(fraction * 100)
    );
  },
  get: (id: string, format: TranscriptFormat) =>
    apiJson<QuickTranscription>(`${path(id)}?format=${format}`),
  preview: (id: string, format: TranscriptFormat) =>
    apiJson<{
      content: string;
      next_offset: number | null;
      total_chars: number;
    }>(`${path(id)}/result?format=${format}&offset=0&limit=32768`),
  cancel: (id: string) =>
    apiJson<QuickTranscription>(`${path(id)}/cancel`, {
      method: 'POST',
      body: '{}'
    }),
  delete: (id: string) =>
    apiJson<QuickTranscription>(path(id), { method: 'DELETE' }),
  download: (id: string, format: TranscriptFormat) =>
    `${path(id)}/result?format=${format}`
};
