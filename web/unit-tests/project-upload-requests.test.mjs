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
const { uploadManagedFile } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('ordinary multipart uploads retain their default request shape', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    requests.push(args);
    return Response.json({ artifact_id: 'artifact' });
  });
  await uploadManagedFile(
    new File(['source'], 'source.txt'),
    'project-session'
  );
  assert.equal(requests.length, 1);
  const [path, request] = requests[0];
  assert.equal(path, '/api/v1/uploads?session_id=project-session');
  assert.equal(request.body.get('session_id'), 'project-session');
  assert.equal(request.body.has('attach_to_session'), false);
});

test('a staged small upload retains a project target without selecting the source', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    requests.push(args);
    return Response.json({ artifact_id: 'artifact', attachment: null });
  });
  await uploadManagedFile(
    new File(['candidate'], 'candidate.txt'),
    'project-session',
    undefined,
    undefined,
    { attachToSession: false }
  );
  const [path, request] = requests[0];
  assert.equal(path, '/api/v1/uploads?session_id=project-session');
  assert.equal(request.body.get('attach_to_session'), 'false');
  assert.equal(request.body.has('workspace_id'), false);
});

test('large staged uploads carry the target through initialization, bounded chunks and completion', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (path, request) => {
    requests.push([path, request]);
    return Response.json(
      path.endsWith('/init')
        ? {
            id: 'upload',
            chunk_size: 8 * 1024 * 1024,
            chunk_count: 5,
            received: []
          }
        : { artifact_id: 'artifact', attachment: null }
    );
  });
  const size = 33 * 1024 * 1024;
  const file = {
    name: 'recording.wav',
    type: 'audio/wav',
    size,
    slice: (start, end) => new Blob([new Uint8Array(end - start)])
  };
  const progress = [];
  await uploadManagedFile(
    file,
    'project-session',
    (value) => progress.push(value),
    undefined,
    { attachToSession: false }
  );
  assert.equal(requests.length, 7);
  const initialization = JSON.parse(requests[0][1].body);
  assert.equal(initialization.session_id, 'project-session');
  assert.equal(initialization.attach_to_session, false);
  assert.equal('workspace_id' in initialization, false);
  const chunks = requests.slice(1, 6);
  assert.deepEqual(
    chunks.map(([path]) => path),
    [0, 1, 2, 3, 4].map((i) => `/api/v1/uploads/upload/chunks/${i}`)
  );
  assert.equal(
    chunks.reduce((sum, [, request]) => sum + request.body.size, 0),
    size
  );
  assert.equal(requests[6][0], '/api/v1/uploads/upload/complete');
  assert.equal(progress.at(-1), 1);
});
