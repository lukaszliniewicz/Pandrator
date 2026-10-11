import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

async function load(entryPoint) {
  const bundle = await build({
    entryPoints: [entryPoint],
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'browser'
  });
  return import(
    `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
  );
}
const sounds = await load('src/lib/sound-api.ts');
const projects = await load('src/lib/project-sounds-api.ts');
const { ApiMutationAttempt } = await load('src/lib/api.ts');

test('sound JSON mutations reach the server as JSON and retries keep their identity', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    const headers = new Headers(init.headers);
    // The backend only parses JSON when the request declares a JSON media type.
    const body =
      headers.get('Content-Type') === 'application/json'
        ? JSON.parse(init.body)
        : {};
    requests.push({ url, method: init.method, headers, body });
    return Response.json({ item: { id: 'saved-sound' } });
  });
  const generation = {
    prompt: 'A kettle whistling. No speech or music.',
    duration_seconds: 4,
    model_id: 'stable_audio_3_small_sfx_q8_0',
    threads: 4
  };
  const attempt = new ApiMutationAttempt();
  await sounds.generateSound(generation, attempt);
  await sounds.generateSound(generation, attempt);
  assert.deepEqual(requests[0].body, generation);
  assert.equal(requests[0].url, '/api/v1/sounds/generate');
  assert.equal(requests[0].method, 'POST');
  assert.ok(requests[0].headers.get('Idempotency-Key'));
  assert.equal(
    requests[0].headers.get('Idempotency-Key'),
    requests[1].headers.get('Idempotency-Key')
  );
  await sounds.saveSound(
    { candidate_id: 'candidate', name: 'Kettle' },
    new ApiMutationAttempt()
  );
  assert.equal(requests.at(-1).body.name, 'Kettle');
  await sounds.updateSound({ id: 'sound', revision: 3 }, { tags: ['kitchen'] });
  assert.equal(requests.at(-1).method, 'PATCH');
  assert.deepEqual(requests.at(-1).body, {
    expected_revision: 3,
    tags: ['kitchen']
  });
});

test('project sound placement, restore and rendering send revisioned JSON', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(
      new Headers(init.headers).get('Content-Type'),
      'application/json'
    );
    requests.push({ url, body: JSON.parse(init.body) });
    return Response.json({});
  });
  const cue = {
    variant_id: 'variant',
    anchor: { kind: 'before_segment', segment_id: 'segment' },
    playback: { mode: 'overlay', gain_db: -8 },
    included: true
  };
  await projects.changeProjectSound('project', 7, cue);
  assert.deepEqual(requests.at(-1).body, { expected_revision: 7, ...cue });
  await projects.restoreProjectSounds('project', 8, 'old-plan');
  assert.deepEqual(requests.at(-1).body, {
    expected_revision: 8,
    source_plan_id: 'old-plan'
  });
  await projects.runProjectSounds('project', 9, 'render', 'base-audio');
  assert.deepEqual(requests.at(-1).body, {
    expected_revision: 9,
    base_artifact_id: 'base-audio'
  });
});

test('sound imports let the browser supply the multipart boundary', async (t) => {
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    assert.equal(url, '/api/v1/sounds/import');
    assert.equal(new Headers(init.headers).has('Content-Type'), false);
    assert.ok(init.body instanceof FormData);
    assert.equal(init.body.get('file').name, 'kettle.wav');
    return Response.json({ id: 'imported-candidate' });
  });
  await sounds.importSound(
    new File(['audio'], 'kettle.wav'),
    new ApiMutationAttempt()
  );
});
