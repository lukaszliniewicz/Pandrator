import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/generation-admission.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
let imports = 0;
const client = () =>
  import(
    `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text + `\n// reload ${imports++}`).toString('base64')}`
  );
function storage(t) {
  const values = new Map();
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key)
    }
  });
  t.after(() =>
    previous
      ? Object.defineProperty(globalThis, 'localStorage', previous)
      : delete globalThis.localStorage
  );
  return values;
}

test('an uncertain admission retry keeps the key across a reload and delivers the exact run', async (t) => {
  const values = storage(t);
  const requests = [];
  let failed = false;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    requests.push({ url, init });
    if (init.method === 'POST') {
      if (!failed) {
        failed = true;
        throw new TypeError('lost acceptance response');
      }
      return Response.json({ job_id: 'preparation-a' }, { status: 202 });
    }
    if (url.includes('/jobs/'))
      return Response.json({
        status: 'succeeded',
        result_json: { generation_run_id: 'run-a' }
      });
    return Response.json({
      id: 'run-a',
      job_id: 'generation-a',
      status: 'queued'
    });
  });
  const body = {
    operation: 'generate',
    settings: { voice: 'private-narrator-direction' }
  };
  await assert.rejects(
    (await client()).admitGeneration('session-a', body),
    /lost acceptance response/
  );
  const saved = values.get('pandrator:generation-admission:session-a');
  assert.ok(saved);
  assert.ok(!saved.includes('private-narrator-direction'));
  assert.equal(
    (await (await client()).admitGeneration('session-a', body)).id,
    'run-a'
  );
  assert.equal(
    requests[0].init.headers.get('Idempotency-Key'),
    requests[1].init.headers.get('Idempotency-Key')
  );
  assert.equal(requests.at(-1).url, '/api/v1/generation-runs/run-a');
  assert.equal(values.size, 0);
});

test('an accepted work handle survives interrupted polling without another POST', async (t) => {
  const values = storage(t);
  const requests = [];
  let interrupted = false;
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    requests.push({ url, init });
    if (init.method === 'POST')
      return Response.json({ job_id: 'preparation-b' });
    if (url.includes('/jobs/')) {
      if (!interrupted) {
        interrupted = true;
        throw new DOMException('closed dialog', 'AbortError');
      }
      return Response.json({
        status: 'running',
        progress_detail: 'Generation queued',
        result_json: { generation_run_id: 'run-b' }
      });
    }
    return Response.json({ id: 'run-b', job_id: 'generation-b' });
  });
  const body = { operation: 'generate' };
  await assert.rejects(
    (await client()).admitGeneration('session-b', body),
    /closed dialog/
  );
  assert.equal(
    JSON.parse(values.get('pandrator:generation-admission:session-b')).jobId,
    'preparation-b'
  );
  const progress = [];
  await (
    await client()
  ).admitGeneration('session-b', body, {
    progress: (value) => progress.push(value)
  });
  assert.equal(requests.filter(({ init }) => init.method === 'POST').length, 1);
  assert.equal(progress[0].jobId, 'preparation-b');
});

test('terminal preparation conflicts retain their code and details, and permit a new attempt', async (t) => {
  const values = storage(t);
  let posts = 0;
  const keys = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    if (init.method === 'POST') {
      posts++;
      keys.push(init.headers.get('Idempotency-Key'));
      return Response.json({ job_id: `preparation-${posts}` });
    }
    return Response.json({
      status: 'failed',
      error_message: 'Refresh the preview.',
      result_json: {
        code: 'generation_admission_stale_inputs',
        details: { speech_plan_revision_id: 'revision-old' }
      }
    });
  });
  const { admitGeneration } = await client();
  for (let attempt = 0; attempt < 2; attempt++) {
    await assert.rejects(
      admitGeneration('session-c', { operation: 'generate' }),
      (error) => {
        assert.equal(error.code, 'generation_admission_stale_inputs');
        assert.equal(error.details.speech_plan_revision_id, 'revision-old');
        return true;
      }
    );
    assert.equal(values.size, 0);
  }
  assert.notEqual(keys[0], keys[1]);
});
