import { apiResponse } from './api';

const TEXT_PREVIEW_BYTES = 2 * 1024 * 1024;
export const TEXT_PREVIEW_PAGE_SIZE = 20_000;

/** Keep a surrogate pair on the same page, with no duplicated or missing text. */
export function textPreviewPage(
  text: string,
  page: number,
  size = TEXT_PREVIEW_PAGE_SIZE
) {
  let start = Math.max(0, page) * size;
  let end = Math.min(start + size, text.length);
  const lowSurrogate = (index: number) => {
    const code = text.charCodeAt(index);
    return code >= 0xdc00 && code <= 0xdfff;
  };
  if (start > 0 && lowSurrogate(start)) start--;
  if (end < text.length && lowSurrogate(end)) end--;
  return text.slice(start, end);
}

/** Bound memory even if a server ignores Range; do not diff a warning as text. */
export async function loadTextPreview(
  url: string,
  options: { signal?: AbortSignal; byteLimit?: number } = {}
): Promise<{ text: string; truncated: boolean }> {
  const limit = options.byteLimit ?? TEXT_PREVIEW_BYTES;
  const response = await apiResponse(url, {
    signal: options.signal,
    headers: { Range: `bytes=0-${limit}` }
  });
  const reader = response.body?.getReader();
  if (!reader) return { text: '', truncated: false };
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (length <= limit) {
      const { value, done } = await reader.read();
      if (done) break;
      const chunk = value.subarray(0, limit + 1 - length);
      chunks.push(chunk);
      length += chunk.length;
    }
  } finally {
    await reader.cancel();
    reader.releaseLock();
  }
  const range = response.headers
    .get('Content-Range')
    ?.match(/^bytes \d+-(\d+)\/(\d+)$/i);
  const truncated =
    length > limit || Boolean(range && Number(range[1]) + 1 < Number(range[2]));
  const bytes = new Uint8Array(Math.min(length, limit));
  let offset = 0;
  for (const chunk of chunks) {
    const part = chunk.subarray(0, bytes.length - offset);
    bytes.set(part, offset);
    offset += part.length;
  }
  // Streaming decode retains an incomplete trailing code point instead of replacing it.
  return {
    text: new TextDecoder().decode(bytes, { stream: truncated }),
    truncated
  };
}
