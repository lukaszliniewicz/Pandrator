export type SessionRecord = {
  id: string;
  name: string;
  storage_key: string;
  workflow_kind: 'audiobook' | 'subtitles' | 'voiceover';
  source_language: string;
  target_language: string | null;
  workflow_preset: string;
  included_stages_json: string[];
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
};

export type JobRecord = {
  id: string;
  kind: string;
  session_id?: string | null;
  payload_json?: Record<string, unknown>;
  status: string;
  progress: number;
  error_message?: string | null;
  result_json?: Record<string, unknown> | null;
  created_at: string;
};

let csrfToken = '';

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export function setCsrfToken(value: string | null | undefined) {
  csrfToken = value ?? '';
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has('Content-Type') && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (csrfToken && init.method && !['GET', 'HEAD'].includes(init.method.toUpperCase())) {
    headers.set('X-CSRF-Token', csrfToken);
  }
  const response = await fetch(`/api/v1${path}`, { ...init, headers });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      String(payload?.error?.code ?? 'request_failed'),
      String(payload?.error?.message ?? `Request failed (${response.status})`),
      payload?.error?.details
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function exchangeBootstrapToken(token: string) {
  const result = await api<{ authenticated: boolean; csrf_token: string }>('/auth/bootstrap', {
    method: 'POST',
    body: JSON.stringify({ token })
  });
  setCsrfToken(result.csrf_token);
  return result;
}

export async function uploadManagedFile(file: File, sessionId?: string, onProgress?: (fraction: number) => void) {
  if (file.size <= 32 * 1024 * 1024) {
    const form = new FormData();
    if (sessionId) form.set('session_id', sessionId);
    form.set('file', file);
    const result = await api<Record<string, any>>('/uploads', { method: 'POST', body: form });
    onProgress?.(1);
    return result;
  }
  const upload = await api<{ id: string; chunk_size: number; chunk_count: number; received: number[] }>('/uploads/init', {
    method: 'POST',
    body: JSON.stringify({ filename: file.name, size_bytes: file.size, mime_type: file.type || null, session_id: sessionId || null })
  });
  const received = new Set(upload.received);
  for (let index = 0; index < upload.chunk_count; index += 1) {
    if (received.has(index)) continue;
    const start = index * upload.chunk_size;
    const body = file.slice(start, Math.min(file.size, start + upload.chunk_size));
    await api(`/uploads/${upload.id}/chunks/${index}`, { method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body });
    onProgress?.((index + 1) / upload.chunk_count);
  }
  return api<Record<string, any>>(`/uploads/${upload.id}/complete`, { method: 'POST' });
}

