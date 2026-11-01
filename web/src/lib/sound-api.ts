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
function workspacePath(path: string, workspaceId?: string) {
  if (!workspaceId) return path;
  return `${path}${path.includes('?') ? '&' : '?'}${new URLSearchParams({ workspace_id: workspaceId })}`;
}
export const soundCandidate = (id: string, workspaceId?: string) =>
  apiJson<SoundCandidate>(
    workspacePath(`/sounds/candidates/${encodeURIComponent(id)}`, workspaceId)
  );
export const soundCandidates = (cursor?: string, workspaceId?: string) =>
  apiJson<SoundPage<SoundCandidate>>(
    workspacePath(
      `/sounds/candidates?${new URLSearchParams({ limit: '20', ...(cursor ? { cursor } : {}) })}`,
      workspaceId
    )
  );
export const soundList = (
  query = '',
  cursor?: string,
  archived = false,
  workspaceId?: string
) =>
  apiJson<SoundPage<SoundItem>>(
    workspacePath(
      `/sounds?${new URLSearchParams({ query, limit: '40', archived: String(archived), ...(cursor ? { cursor } : {}) })}`,
      workspaceId
    )
  );
export const soundDetail = (
  id: string,
  cursor?: string,
  workspaceId?: string
) =>
  apiJson<SoundDetail>(
    workspacePath(
      `/sounds/${encodeURIComponent(id)}?${new URLSearchParams({ limit: '20', ...(cursor ? { cursor } : {}) })}`,
      workspaceId
    )
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
  attempt: ApiMutationAttempt,
  workspaceId?: string
) =>
  mutate<SoundGenerationReceipt>(
    workspacePath('/sounds/generate', workspaceId),
    body,
    attempt
  );
export const saveSound = (
  body: {
    candidate_id: string;
    name?: string;
    tags?: string[];
    item_id?: string;
    expected_revision?: number;
  },
  attempt: ApiMutationAttempt,
  workspaceId?: string
) =>
  mutate<{ item: SoundItem; item_id: string; reused: boolean }>(
    workspacePath('/sounds/save', workspaceId),
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
  },
  workspaceId?: string
) =>
  mutate<SoundItem>(
    workspacePath(`/sounds/${encodeURIComponent(item.id)}`, workspaceId),
    { expected_revision: item.revision, ...changes },
    undefined,
    'PATCH'
  );
export const discardSound = (id: string, workspaceId?: string) =>
  apiJson(
    workspacePath(`/sounds/candidates/${encodeURIComponent(id)}`, workspaceId),
    {
      method: 'DELETE',
      headers: new ApiMutationAttempt().headersFor(`discard-sound-${id}`, {})
    }
  );
export const cancelSound = (jobId: string) =>
  apiJson(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
export const importSound = (
  file: File,
  attempt: ApiMutationAttempt,
  workspaceId?: string
) => {
  const form = new FormData();
  form.set('file', file);
  const path = workspacePath('/sounds/import', workspaceId);
  return apiJson<SoundCandidate>(path, {
    method: 'POST',
    headers: attempt.headersFor(path, {
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
