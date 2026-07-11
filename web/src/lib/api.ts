export type SessionRecord = {
  id: string;
  name: string;
  storage_key: string;
  workflow_kind: 'audiobook' | 'subtitles' | 'voiceover';
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
  status: string;
  progress: number;
  error_message?: string | null;
  result_json?: Record<string, unknown> | null;
  created_at: string;
};

let csrfToken = '';

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
    throw new Error(payload?.error?.message ?? `Request failed (${response.status})`);
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

