import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  stdin: {
    contents:
      'export { artifactApi } from "./src/lib/domain-api.ts"; export { setCsrfToken } from "./src/lib/api.ts";',
    resolveDir: process.cwd()
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { artifactApi, setCsrfToken } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

const previews = [
  ['audio', (signal) => artifactApi.audioPreview('clip id', signal)],
  ['video', (signal) => artifactApi.videoPreview('clip id', signal)],
  ['waveform', (signal) => artifactApi.waveform('clip id', 128, signal)],
  [
    'window',
    (signal) => artifactApi.waveformWindow('clip id', 10, 1010, 256, signal)
  ]
];

function missing(canPrepare) {
  return Response.json(
    {
      error: {
        code: 'preview_preparation_required',
        message: 'This preview has not been prepared.',
        details: { can_prepare: canPrepare }
      }
    },
    { status: 409 }
  );
}

for (const [name, invoke] of previews) {
  test(`${name}: cached media and active jobs remain reads`, async (t) => {
    const requests = [];
    const responses = [
      Response.json({ status: 'ready' }),
      Response.json({ status: 'running', job_id: 'existing' }, { status: 202 })
    ];
    t.mock.method(globalThis, 'fetch', async (url, init) => {
      requests.push({ url, ...init });
      return responses.shift();
    });
    assert.deepEqual(await invoke(), { status: 'ready' });
    assert.deepEqual(await invoke(), { status: 'running', job_id: 'existing' });
    assert.deepEqual(
      requests.map((row) => row.method),
      ['GET', 'GET']
    );
  });

  test(`${name}: authorized miss prepares once with the same parameters and CSRF`, async (t) => {
    const requests = [];
    setCsrfToken('synthetic-cookie-csrf');
    t.after(() => setCsrfToken(''));
    t.mock.method(globalThis, 'fetch', async (url, init) => {
      requests.push({ url, ...init });
      return requests.length === 1
        ? missing(true)
        : Response.json(
            { status: 'queued', job_id: 'admitted' },
            { status: 202 }
          );
    });
    assert.deepEqual(await invoke(), { status: 'queued', job_id: 'admitted' });
    assert.deepEqual(
      requests.map((row) => row.method),
      ['GET', 'POST']
    );
    assert.equal(requests[0].url, requests[1].url);
    assert.match(requests[0].url, /clip%20id/);
    assert.equal(new Headers(requests[0].headers).has('X-CSRF-Token'), false);
    assert.equal(
      new Headers(requests[1].headers).get('X-CSRF-Token'),
      'synthetic-cookie-csrf'
    );
    assert.ok(new Headers(requests[1].headers).get('Idempotency-Key'));
    if (name === 'window') {
      assert.match(requests[0].url, /points=256&start_ms=10&end_ms=1010$/);
    }
  });

  test(`${name}: false or malformed hints and permission errors never prepare`, async (t) => {
    const responses = [
      missing(false),
      missing(undefined),
      missing('true'),
      Response.json(
        { error: { code: 'source_changed', details: { can_prepare: true } } },
        { status: 409 }
      ),
      Response.json({ error: { code: 'scope_denied' } }, { status: 403 })
    ];
    const methods = [];
    t.mock.method(globalThis, 'fetch', async (_url, init) => {
      methods.push(init.method);
      return responses.shift();
    });
    for (let index = 0; index < 5; index += 1) await assert.rejects(invoke());
    assert.deepEqual(methods, ['GET', 'GET', 'GET', 'GET', 'GET']);
  });

  test(`${name}: cancellation and failed writes do not start another preparation`, async (t) => {
    const controller = new AbortController();
    const requests = [];
    const fetch = t.mock.method(globalThis, 'fetch', async (_url, init) => {
      requests.push(init.method);
      controller.abort();
      return missing(true);
    });
    await assert.rejects(invoke(controller.signal), { name: 'AbortError' });
    assert.deepEqual(requests, ['GET']);
    fetch.mock.restore();
    const writes = [];
    t.mock.method(globalThis, 'fetch', async (_url, init) => {
      writes.push(init.method);
      if (init.method === 'GET') return missing(true);
      throw new TypeError('Network interrupted');
    });
    await assert.rejects(invoke(), TypeError);
    assert.deepEqual(writes, ['GET', 'POST']);
  });
}
