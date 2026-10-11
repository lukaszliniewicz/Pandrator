import { ApiError, ApiMutationAttempt, apiJson } from './api';
import type { GenerationRun, JobRecord } from './api-models';

export type AdmissionProgress = {
  jobId: string;
  status: string;
  detail: string;
};
type SavedAttempt = { signature: string; key: string; jobId?: string };
const attempts = new Map<string, SavedAttempt>();

function loadAttempt(storageKey: string): SavedAttempt | undefined {
  try {
    const value = JSON.parse(localStorage.getItem(storageKey) ?? 'null');
    if (
      value &&
      typeof value.signature === 'string' &&
      typeof value.key === 'string' &&
      /^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$/.test(value.key) &&
      (value.jobId === undefined ||
        (typeof value.jobId === 'string' && value.jobId.length <= 160))
    )
      return value;
  } catch {
    /* Storage can be disabled. */
  }
  return attempts.get(storageKey);
}
function saveAttempt(storageKey: string, value: SavedAttempt) {
  attempts.set(storageKey, value);
  try {
    localStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    /* In-memory retry remains. */
  }
}
function clearAttempt(storageKey: string) {
  attempts.delete(storageKey);
  try {
    localStorage.removeItem(storageKey);
  } catch {
    /* Storage is optional. */
  }
}
function pause(signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
      reject(new DOMException('Polling stopped', 'AbortError'));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', abort);
      resolve();
    }, 1000);
    if (signal?.aborted) abort();
    else signal?.addEventListener('abort', abort, { once: true });
  });
}

/** Persist retry identity and work IDs, never source text or request settings. */
export async function admitGeneration(
  sessionId: string,
  body: Record<string, unknown>,
  options: {
    progress?: (value: AdmissionProgress) => void;
    signal?: AbortSignal;
    attempt?: ApiMutationAttempt;
  } = {}
): Promise<GenerationRun> {
  const target =
    '/api/v1/sessions/' +
    encodeURIComponent(sessionId) +
    '/generation-admissions';
  const digest = await crypto.subtle.digest(
    'SHA-256',
    new TextEncoder().encode(JSON.stringify(body))
  );
  const signature = Array.from(new Uint8Array(digest), (n) =>
    n.toString(16).padStart(2, '0')
  ).join('');
  const storageKey = 'pandrator:generation-admission:' + sessionId;
  let saved = loadAttempt(storageKey);
  if (!saved || saved.signature !== signature) {
    const headers = (options.attempt ?? new ApiMutationAttempt()).headersFor(
      target,
      body
    );
    saved = { signature, key: headers['Idempotency-Key'] };
    saveAttempt(storageKey, saved);
  }
  if (!saved.jobId) {
    const receipt = await apiJson<{ job_id: string }>(target, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Idempotency-Key': saved.key
      },
      body: JSON.stringify(body),
      signal: options.signal
    });
    if (!receipt.job_id)
      throw new Error('Generation admission returned no work handle.');
    saved.jobId = receipt.job_id;
    saveAttempt(storageKey, saved);
  }
  const jobId = saved.jobId;
  const deadline = Date.now() + 120_000;
  while (true) {
    const job = await apiJson<JobRecord>(
      '/api/v1/jobs/' + encodeURIComponent(jobId),
      { signal: options.signal }
    );
    options.progress?.({
      jobId,
      status: job.status,
      detail: job.progress_detail || 'Preparing generation'
    });
    const result = job.result_json;
    const runId =
      typeof result?.generation_run_id === 'string'
        ? result.generation_run_id
        : null;
    if (runId) {
      const run = await apiJson<GenerationRun>(
        '/api/v1/generation-runs/' + encodeURIComponent(runId),
        { signal: options.signal }
      );
      clearAttempt(storageKey);
      return run;
    }
    if (['failed', 'canceled', 'cancelled', 'succeeded'].includes(job.status)) {
      clearAttempt(storageKey);
      const error = (result?.error ?? result) as
        { code?: string; message?: string; details?: unknown } | undefined;
      throw new ApiError(
        409,
        error?.code || job.error_code || 'generation_admission_failed',
        error?.message || job.error_message || 'Generation preparation failed.',
        error?.details
      );
    }
    if (Date.now() >= deadline)
      throw new Error(
        'Preparation is continuing. Work ' +
          jobId +
          ' can be resumed with the same selection.'
      );
    await pause(options.signal);
  }
}
