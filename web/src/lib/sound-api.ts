import { apiJson, ApiMutationAttempt } from './api';

export type SoundCandidate = {
  id: string;
  status: string;
  job_id: string | null;
  artifact_id: string | null;
  item_id: string | null;
  duration_seconds: number | null;
  sample_rate: number | null;
  channels: number | null;
  created_at: string;
  provenance: {
    operation?: string;
    provider?: string;
    model_id?: string;
    prompt?: string;
    seed?: number;
    duration_seconds?: number;
    backend?: string;
    display_name?: string;
    kind?: 'sound' | 'music';
    lyrics?: string;
    parameters?: Record<string, string | number | boolean>;
    threads?: number;
    num_inference_steps?: number;
    guidance_scale?: number;
  };
};
export type SoundItem = {
  id: string;
  name: string;
  tags: string[];
  revision: number;
  archived: boolean;
  preferred_variant: SoundCandidate | null;
  created_at: string;
  updated_at: string;
};
export type SoundPage<T> = { items: T[]; next_cursor: string | null };
export type SoundDetail = SoundItem & { variants: SoundPage<SoundCandidate> };
export type SoundKind = 'sound' | 'music';
export type SoundBackend = 'cpu' | 'vulkan' | 'cuda';
export type SoundParameter = {
  type: 'integer' | 'number' | 'boolean' | 'string';
  default?: string | number | boolean;
  minimum?: number;
  exclusive_minimum?: number;
  maximum?: number;
  options?: string[];
  label: string;
  description?: string;
};
export type SoundModel = {
  id: string;
  label: string;
  family: string;
  cached: boolean;
  kinds: SoundKind[];
  description: string;
  source_url?: string;
  backends: SoundBackend[];
  notes?: string;
  duration: {
    minimum: number;
    maximum: number;
    default: number;
    description?: string;
  };
  supports_lyrics: boolean;
  requires_lyrics?: boolean;
  seed_maximum: number;
  download_bytes: number;
  available?: boolean;
  reason?: string;
  parameters: Record<string, SoundParameter>;
  instrumental_adapter?: {
    id: string;
    repository: string;
    revision: string;
    filename: string;
    size_bytes: number;
    sha256: string;
    license: string;
    source_url: string;
  };
};
export type SoundCatalogue = {
  runtime: {
    available: boolean;
    version: string | null;
    reason?: string | null;
    backends?: SoundBackend[];
  };
  models: SoundModel[];
  unavailable_models?: {
    id: string;
    label: string;
    family: string;
    reason: string;
  }[];
  defaults: { model_id: string; duration_seconds: number; threads: number };
};
export type SoundGeneration = {
  prompt: string;
  duration_seconds: number;
  model_id: string;
  seed?: number;
  threads?: number;
  kind?: SoundKind;
  backend?: SoundBackend;
  lyrics?: string;
  parameters?: Record<string, string | number | boolean>;
  candidate_count?: number;
};
export type SoundCandidateReceipt = {
  candidate_id: string;
  job_id: string | null;
  item_id?: string | null;
  status: string;
  cache_hit: boolean;
  seed: number;
};
export type SoundGenerationReceipt = {
  candidate_id: string;
  job_id: string | null;
  status: string;
  cache_hit: boolean;
  item_id?: string | null;
  resolved_settings: SoundGeneration & { seed: number };
  candidate_count?: number;
  candidates?: SoundCandidateReceipt[];
};

export const soundAudioUrl = (artifactId: string) =>
  `/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`;
export const soundCatalogue = () =>
  apiJson<SoundCatalogue>('/sounds/catalogue');
export const soundCandidate = (id: string) =>
  apiJson<SoundCandidate>(`/sounds/candidates/${encodeURIComponent(id)}`);
export const soundCandidates = (cursor?: string) =>
  apiJson<SoundPage<SoundCandidate>>(
    `/sounds/candidates?${new URLSearchParams({ limit: '20', ...(cursor ? { cursor } : {}) })}`
  );
export const soundList = (query = '', cursor?: string, archived = false) =>
  apiJson<SoundPage<SoundItem>>(
    `/sounds?${new URLSearchParams({ query, limit: '40', archived: String(archived), ...(cursor ? { cursor } : {}) })}`
  );
export const soundDetail = (id: string, cursor?: string) =>
  apiJson<SoundDetail>(
    `/sounds/${encodeURIComponent(id)}?${new URLSearchParams({ limit: '20', ...(cursor ? { cursor } : {}) })}`
  );

function mutate<T>(
  path: string,
  body: unknown,
  attempt?: ApiMutationAttempt,
  method = 'POST'
) {
  return apiJson<T>(path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(attempt ?? new ApiMutationAttempt()).headersFor(path, body)
    },
    body: JSON.stringify(body)
  });
}
export const generateSound = (
  body: SoundGeneration,
  attempt: ApiMutationAttempt
) => mutate<SoundGenerationReceipt>('/sounds/generate', body, attempt);
export const saveSound = (
  body: {
    candidate_id: string;
    name?: string;
    tags?: string[];
    item_id?: string;
    expected_revision?: number;
  },
  attempt: ApiMutationAttempt
) =>
  mutate<{ item: SoundItem; item_id: string; reused: boolean }>(
    '/sounds/save',
    body,
    attempt
  ).then((receipt) => receipt.item);
export const updateSound = (
  item: SoundItem,
  changes: {
    name?: string;
    tags?: string[];
    archived?: boolean;
    preferred_variant_id?: string;
  }
) =>
  mutate<SoundItem>(
    `/sounds/${encodeURIComponent(item.id)}`,
    { expected_revision: item.revision, ...changes },
    undefined,
    'PATCH'
  );
export const discardSound = (id: string) =>
  apiJson(`/sounds/candidates/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    headers: new ApiMutationAttempt().headersFor(`discard-sound-${id}`, {})
  });
export const cancelSound = (jobId: string) =>
  apiJson(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
export const importSound = (file: File, attempt: ApiMutationAttempt) => {
  const form = new FormData();
  form.set('file', file);
  return apiJson<SoundCandidate>('/sounds/import', {
    method: 'POST',
    headers: attempt.headersFor('/sounds/import', {
      name: file.name,
      size: file.size,
      modified: file.lastModified
    }),
    body: form
  });
};
export const soundTags = (text: string) => [
  ...new Set(
    text
      .split(',')
      .map((value) => value.trim())
      .filter(Boolean)
  )
];
export const soundPending = (candidate: SoundCandidate | null) =>
  candidate !== null &&
  ['queued', 'running', 'pending', 'retrying'].includes(candidate.status);
