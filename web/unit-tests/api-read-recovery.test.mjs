import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/api.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { apiJson, ApiError } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('a network change retries one read with the same request and recovers', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    requests.push(args);
    if (requests.length === 1) throw new TypeError('Failed to fetch');
    return Response.json({ recovered: true });
  });
  assert.deepEqual(await apiJson('/sessions/example'), { recovered: true });
  assert.equal(requests.length, 2);
  assert.equal(requests[0][0], '/api/v1/sessions/example');
  assert.equal(requests[0][1], requests[1][1]);
});

test('persistent read failures stop after two attempts', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => {
    throw new TypeError('Failed to fetch');
  });
  await assert.rejects(apiJson('/sessions/example'), TypeError);
  assert.equal(fetch.mock.callCount(), 2);
});

test('writes, HTTP errors and aborted reads are not retried', async (t) => {
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    const fetch = t.mock.method(globalThis, 'fetch', async () => {
      throw new TypeError('Failed to fetch');
    });
    await assert.rejects(apiJson('/sessions/example', { method }), TypeError);
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.restore();
  }
  const http = t.mock.method(globalThis, 'fetch', async () =>
    Response.json({ error: { message: 'Unavailable' } }, { status: 503 })
  );
  await assert.rejects(apiJson('/sessions/example'), ApiError);
  assert.equal(http.mock.callCount(), 1);
  http.mock.restore();
  const controller = new AbortController();
  const abort = t.mock.method(globalThis, 'fetch', async () => {
    controller.abort();
    throw new TypeError('Failed to fetch');
  });
  await assert.rejects(
    apiJson('/sessions/example', { signal: controller.signal }),
    TypeError
  );
  assert.equal(abort.mock.callCount(), 1);
});

test('cancellation during recovery prevents the second read', async (t) => {
  const controller = new AbortController();
  const fetch = t.mock.method(globalThis, 'fetch', async () => {
    setTimeout(() => controller.abort(), 20);
    throw new TypeError('Failed to fetch');
  });
  await assert.rejects(
    apiJson('/sessions/example', { signal: controller.signal }),
    { name: 'AbortError' }
  );
  assert.equal(fetch.mock.callCount(), 1);
});
