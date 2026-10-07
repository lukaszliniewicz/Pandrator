import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  stdin: {
    contents:
      "export * from './src/lib/generation-recast'; export { ApiMutationAttempt } from './src/lib/api';",
    resolveDir: process.cwd(),
    loader: 'ts'
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const {
  ApiMutationAttempt,
  previewRecast,
  startRecast,
  previewUndoRecast,
  undoRecast
} = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('recast and undo requests send JSON bodies and replay unchanged acceptance', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, request) => {
    requests.push({ url, request });
    return Response.json({
      id: 'run',
      session_id: 'project',
      run: { id: 'run', session_id: 'project' }
    });
  });
  const selection = {
    selector_keys: ['character:fred'],
    force: true,
    include_missing: false
  };
  const start = { ...selection, expected_selection_hash: 'selection-hash' };
  const attempt = new ApiMutationAttempt();
  await previewRecast('project', selection);
  await startRecast('project', start, attempt);
  await startRecast('project', start, attempt);
  await previewUndoRecast('run');
  const undoAttempt = new ApiMutationAttempt();
  await undoRecast('run', { selection_hash: 'undo-hash' }, undoAttempt);
  await undoRecast('run', { selection_hash: 'undo-hash' }, undoAttempt);

  for (const { request } of requests) {
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.get('Content-Type'), 'application/json');
    assert.ok(request.headers.get('Idempotency-Key'));
  }
  assert.deepEqual(JSON.parse(requests[0].request.body), selection);
  assert.deepEqual(JSON.parse(requests[1].request.body), start);
  assert.deepEqual(JSON.parse(requests[3].request.body), {});
  assert.deepEqual(JSON.parse(requests[4].request.body), {
    expected_selection_hash: 'undo-hash'
  });
  assert.equal(
    requests[1].request.headers.get('Idempotency-Key'),
    requests[2].request.headers.get('Idempotency-Key')
  );
  assert.equal(
    requests[4].request.headers.get('Idempotency-Key'),
    requests[5].request.headers.get('Idempotency-Key')
  );
});
