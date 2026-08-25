import type { components, paths } from './api.generated';

type ApiPath = keyof paths;
type HttpMethod = 'get' | 'put' | 'post' | 'delete' | 'patch';
type ApiMethod<P extends ApiPath> = {
  [M in HttpMethod]: paths[P][M] extends never | undefined ? never : M;
}[HttpMethod];
type ApiOperation<P extends ApiPath, M extends ApiMethod<P>> = NonNullable<
  paths[P][M]
>;
type OperationParameters<
  P extends ApiPath,
  M extends ApiMethod<P>,
  K extends 'path' | 'query' | 'header'
> =
  ApiOperation<P, M> extends { parameters: infer Parameters }
    ? K extends keyof Parameters
      ? Parameters[K]
      : never
    : never;
type OperationBody<P extends ApiPath, M extends ApiMethod<P>> =
  ApiOperation<P, M> extends {
    requestBody: { content: { 'application/json': infer Body } };
  }
    ? Body
    : never;
type RequestBody<P extends ApiPath, M extends ApiMethod<P>> = [
  OperationBody<P, M>
] extends [never]
  ? Record<string, unknown>
  : OperationBody<P, M>;

export type ApiSchema<Name extends keyof components['schemas']> =
  components['schemas'][Name];

export type TypedApiOptions<P extends ApiPath, M extends ApiMethod<P>> = Omit<
  RequestInit,
  'method' | 'body' | 'headers'
> & {
  path?: OperationParameters<P, M, 'path'>;
  query?: OperationParameters<P, M, 'query'> | URLSearchParams;
  headers?: HeadersInit;
  body?: RequestBody<P, M> | FormData | Blob | ArrayBuffer;
};

let csrfToken = '';

export class ApiError extends Error {
  status: number;
  code: string;
  details: unknown;
  requestId: string;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: unknown,
    requestId = ''
  ) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

export function setCsrfToken(value: string | null | undefined) {
  csrfToken = value ?? '';
}

function createIdempotencyKey() {
  if (typeof globalThis.crypto?.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join(
    ''
  );
}

function interpolatePath(
  template: string,
  parameters?: Record<string, unknown>
) {
  if (!parameters) return template;
  return template.replace(/\{([^}]+)\}/g, (_match, key: string) => {
    const value = parameters[key];
    if (value === undefined || value === null) {
      throw new Error(`Missing API path parameter: ${key}`);
    }
    return encodeURIComponent(String(value));
  });
}

function appendQuery(
  path: string,
  query?: Record<string, unknown> | URLSearchParams
) {
  if (!query) return path;
  const values =
    query instanceof URLSearchParams ? query : new URLSearchParams();
  if (!(query instanceof URLSearchParams)) {
    for (const [key, value] of Object.entries(query)) {
      if (value === undefined || value === null) continue;
      if (Array.isArray(value)) {
        for (const item of value) values.append(key, String(item));
      } else {
        values.set(key, String(value));
      }
    }
  }
  const suffix = values.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function serializeBody(body: unknown, headers: Headers) {
  if (body === undefined || body === null) return undefined;
  if (
    body instanceof FormData ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    typeof body === 'string'
  ) {
    return body;
  }
  if (!headers.has('Content-Type'))
    headers.set('Content-Type', 'application/json');
  return JSON.stringify(body);
}

const XHR_NETWORK_ERROR_MESSAGE =
  'The upload connection was interrupted. Check free disk space and the Pandrator Manager + XTTS logs, then retry.';

function errorFromPayload(status: number, payload: unknown, requestId = '') {
  const envelope =
    payload && typeof payload === 'object'
      ? (payload as { error?: unknown }).error
      : undefined;
  const error =
    envelope && typeof envelope === 'object'
      ? (envelope as {
          code?: unknown;
          message?: unknown;
          details?: unknown;
          request_id?: unknown;
        })
      : {};
  return new ApiError(
    status,
    String(error.code ?? 'request_failed'),
    String(error.message ?? `Request failed (${status})`),
    error.details,
    String(error.request_id ?? requestId)
  );
}

async function errorFromResponse(response: Response) {
  const payload: unknown = await response.json().catch(() => undefined);
  return errorFromPayload(
    response.status,
    payload,
    response.headers.get('X-Request-ID') ?? ''
  );
}

export async function apiResponse(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  const method = String(init.method ?? 'GET').toUpperCase();
  const isMutation = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  if (isMutation && !headers.has('Idempotency-Key')) {
    headers.set('Idempotency-Key', createIdempotencyKey());
  }
  if (csrfToken && isMutation) {
    headers.set('X-CSRF-Token', csrfToken);
  }
  const body = serializeBody(init.body, headers);
  const response = await fetch(
    path.startsWith('/api/v1') ? path : `/api/v1${path}`,
    {
      ...init,
      method,
      body,
      headers,
      credentials: init.credentials ?? 'same-origin'
    }
  );
  if (!response.ok) throw await errorFromResponse(response);
  return response;
}

export async function apiJson<T>(
  path: string,
  init: RequestInit = {}
): Promise<T> {
  const response = await apiResponse(path, init);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export type UploadProgressCallback = (fraction: number) => void;
export type UploadTransferCompleteCallback = () => void;

/**
 * Send a JSON-response request with a browser-visible upload progress stream.
 *
 * XHR is used here because fetch does not expose request-body transfer events
 * in the browsers supported by the application. The request keeps the same
 * mutation headers and credential defaults as apiResponse, and deliberately
 * leaves the XHR timeout at its browser default (zero / no timeout).
 */
export function apiJsonUpload<T>(
  path: string,
  init: RequestInit = {},
  onProgress?: UploadProgressCallback,
  onTransferComplete?: UploadTransferCompleteCallback
): Promise<T> {
  const headers = new Headers(init.headers);
  const method = String(init.method ?? 'GET').toUpperCase();
  const isMutation = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  if (isMutation && !headers.has('Idempotency-Key')) {
    headers.set('Idempotency-Key', createIdempotencyKey());
  }
  if (csrfToken && isMutation) {
    headers.set('X-CSRF-Token', csrfToken);
  }
  const body = serializeBody(init.body, headers);
  const url = path.startsWith('/api/v1') ? path : `/api/v1${path}`;

  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const credentials = init.credentials ?? 'same-origin';
    let transferComplete = false;
    const markTransferComplete = () => {
      if (transferComplete) return;
      transferComplete = true;
      onProgress?.(1);
      onTransferComplete?.();
    };
    const rejectNetworkError = () =>
      reject(new ApiError(0, 'network_error', XHR_NETWORK_ERROR_MESSAGE));

    xhr.open(method, url, true);
    xhr.withCredentials = credentials === 'include';
    headers.forEach((value, key) => xhr.setRequestHeader(key, value));
    xhr.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable || event.total <= 0) return;
      onProgress?.(Math.max(0, Math.min(1, event.loaded / event.total)));
    });
    xhr.upload.addEventListener('load', markTransferComplete);
    xhr.addEventListener('load', () => {
      if (xhr.status < 200 || xhr.status >= 300) {
        let payload: unknown;
        try {
          payload = xhr.responseText ? JSON.parse(xhr.responseText) : undefined;
        } catch {
          payload = undefined;
        }
        reject(
          errorFromPayload(
            xhr.status,
            payload,
            xhr.getResponseHeader('X-Request-ID') ?? ''
          )
        );
        return;
      }
      if (xhr.status === 204) {
        resolve(undefined as T);
        return;
      }
      try {
        resolve(JSON.parse(xhr.responseText) as T);
      } catch (caught) {
        reject(caught);
      }
    });
    xhr.addEventListener('error', rejectNetworkError);
    xhr.addEventListener('abort', rejectNetworkError);
    xhr.send(body as XMLHttpRequestBodyInit | null);
  });
}

export async function typedApiJson<
  P extends ApiPath,
  M extends ApiMethod<P>,
  Response
>(
  path: P,
  method: M,
  options: TypedApiOptions<P, M> = {} as TypedApiOptions<P, M>
): Promise<Response> {
  const { path: pathParameters, query, headers, body, ...init } = options;
  const resolvedPath = appendQuery(
    interpolatePath(
      path,
      pathParameters as Record<string, unknown> | undefined
    ),
    query as Record<string, unknown> | URLSearchParams | undefined
  );
  return apiJson<Response>(resolvedPath, {
    ...init,
    method: method.toUpperCase(),
    headers,
    body: body as BodyInit | null | undefined
  });
}

export async function exchangeBootstrapToken(token: string) {
  const result = await typedApiJson<
    '/api/v1/auth/bootstrap',
    'post',
    { authenticated: boolean; csrf_token: string }
  >('/api/v1/auth/bootstrap', 'post', { body: { token } });
  setCsrfToken(result.csrf_token);
  return result;
}

export async function uploadManagedFile(
  file: File,
  sessionId?: string,
  onProgress?: (fraction: number) => void
) {
  if (file.size <= 32 * 1024 * 1024) {
    const form = new FormData();
    if (sessionId) form.set('session_id', sessionId);
    form.set('file', file);
    const result = await typedApiJson<
      '/api/v1/uploads',
      'post',
      Record<string, unknown>
    >('/api/v1/uploads', 'post', { body: form });
    onProgress?.(1);
    return result;
  }
  const upload = await typedApiJson<
    '/api/v1/uploads/init',
    'post',
    { id: string; chunk_size: number; chunk_count: number; received: number[] }
  >('/api/v1/uploads/init', 'post', {
    body: {
      filename: file.name,
      size_bytes: file.size,
      mime_type: file.type || null,
      session_id: sessionId || null,
      sha256: null,
      chunk_size: 8 * 1024 * 1024
    }
  });
  const received = new Set(upload.received);
  for (let index = 0; index < upload.chunk_count; index += 1) {
    if (received.has(index)) continue;
    const start = index * upload.chunk_size;
    const body = file.slice(
      start,
      Math.min(file.size, start + upload.chunk_size)
    );
    await typedApiJson<
      '/api/v1/uploads/{uploadId}/chunks/{index}',
      'put',
      Record<string, unknown>
    >('/api/v1/uploads/{uploadId}/chunks/{index}', 'put', {
      path: { uploadId: upload.id, index },
      headers: { 'Content-Type': 'application/octet-stream' },
      body
    });
    onProgress?.((index + 1) / upload.chunk_count);
  }
  return typedApiJson<
    '/api/v1/uploads/{uploadId}/complete',
    'post',
    Record<string, unknown>
  >('/api/v1/uploads/{uploadId}/complete', 'post', {
    path: { uploadId: upload.id }
  });
}
