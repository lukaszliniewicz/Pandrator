import { apiJson, type ApiMutationAttempt } from './api';
import type { JobRecord } from './api-models';

export type ReferenceSample = {
  id: string;
  artifact_id: string;
  available?: boolean;
};

export function referenceCrop(start: number, end: string) {
  const finish = end.trim() ? Number(end) : undefined;
  if (!Number.isFinite(start) || start < 0)
    throw new Error(
      'The start must be a finite number of seconds, zero or greater.'
    );
  if (finish !== undefined && (!Number.isFinite(finish) || finish <= start))
    throw new Error('The end must be later than the start.');
  return {
    ...(start > 0 ? { start_seconds: start } : {}),
    ...(finish !== undefined ? { end_seconds: finish } : {})
  };
}

export function importReferenceAudio(file: File, attempt: ApiMutationAttempt) {
  if (file.size > 50 * 1024 * 1024)
    throw new Error('Audio imports may not exceed 50 MiB.');
  const path = '/voices/reference-audio/import';
  return apiJson<{ artifact_id: string }>(path, {
    method: 'PUT',
    headers: {
      ...attempt.headersFor(path, {
        name: file.name,
        size: file.size,
        modified: file.lastModified
      }),
      'Content-Type': 'application/octet-stream',
      'X-Audio-Filename': encodeURIComponent(file.name)
    },
    body: file
  });
}

export function prepareReference(
  voiceId: string,
  body: {
    artifact_id: string;
    expected_voice_revision: number;
    language: string;
    transcript?: string;
    transcript_reviewed: boolean;
    start_seconds?: number;
    end_seconds?: number;
  },
  attempt: ApiMutationAttempt
) {
  const path = `/voices/${encodeURIComponent(voiceId)}/samples/from-artifact`;
  return apiJson<JobRecord>(path, {
    method: 'POST',
    headers: attempt.headersFor(path, body),
    body: JSON.stringify(body)
  });
}
