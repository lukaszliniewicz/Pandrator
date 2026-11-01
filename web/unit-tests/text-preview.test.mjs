import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/text-preview.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { loadTextPreview } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

for (const [input, expected, truncated] of [
  ['', '', false],
  ['short', 'short', false],
  ['12345678', '12345678', false],
  ['123456789', '12345678', true],
  ['a字幕a', 'a字幕a', false],
  ['字幕字幕', '字幕', true],
  ['abcd😀z', 'abcd😀', true]
]) {
  test(`byte-limited preview: ${JSON.stringify(input)}`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => new Response(input));
    const result = await loadTextPreview('/synthetic', { byteLimit: 8 });
    assert.deepEqual(result, { text: expected, truncated });
    assert.equal(result.text.includes('\uFFFD'), false);
  });
}

test('a partial response identifies truncation without a sentinel byte', async (t) => {
  t.mock.method(globalThis, 'fetch', async (_url, init) => {
    assert.equal(new Headers(init.headers).get('Range'), 'bytes=0-8');
    return new Response('字幕', {
      status: 206,
      headers: { 'Content-Range': 'bytes 0-5/12' }
    });
  });
  assert.deepEqual(await loadTextPreview('/synthetic', { byteLimit: 8 }), {
    text: '字幕',
    truncated: true
  });
});

test('a server ignoring Range is cancelled once enough bytes arrive', async (t) => {
  let reads = 0;
  let cancelled = false;
  const stream = new ReadableStream({
    pull(controller) {
      reads++;
      controller.enqueue(new TextEncoder().encode('0123456789abcdef'));
    },
    cancel() {
      cancelled = true;
    }
  });
  t.mock.method(globalThis, 'fetch', async () => new Response(stream));
  assert.deepEqual(await loadTextPreview('/synthetic', { byteLimit: 8 }), {
    text: '01234567',
    truncated: true
  });
  assert.equal(cancelled, true);
  assert.ok(reads <= 2, `unexpectedly consumed ${reads} chunks`);
});

test('aborted loads and permission errors preserve the API failure', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => {
    throw new DOMException('Cancelled', 'AbortError');
  });
  await assert.rejects(loadTextPreview('/synthetic'), { name: 'AbortError' });
  t.mock.method(globalThis, 'fetch', async () =>
    Response.json(
      { error: { code: 'not_found', message: 'Artifact not found.' } },
      { status: 404 }
    )
  );
  await assert.rejects(loadTextPreview('/synthetic'), { status: 404 });
});

test('render pages preserve surrogate pairs across an emoji boundary', async () => {
  const { textPreviewPage } = await import(
    `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
  );
  const text = 'abc😀def😀ghi';
  const chunks = Array.from({ length: Math.ceil(text.length / 4) }, (_, page) =>
    textPreviewPage(text, page, 4)
  );
  assert.equal(chunks.join(''), text);
  for (const chunk of chunks) assert.equal(chunk.isWellFormed(), true);
});
