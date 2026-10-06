/*global console */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { Buffer } from 'node:buffer';
import process from 'node:process';
import { build, transform } from 'esbuild';
import { compileModule } from 'svelte/compiler';

// Compile the real runes store/resource. Only transport is replaced.
const bundle = await build({
  stdin: {
    contents:
      "export { WorkflowStore } from './src/lib/workflow-store.svelte.ts'; export { invalidationBus } from './src/lib/invalidation.ts';",
    resolveDir: process.cwd(),
    loader: 'ts'
  },
  bundle: true,
  write: false,
  platform: 'node',
  format: 'esm',
  conditions: ['browser'],
  plugins: [
    {
      name: 'fixtures',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/domain-api$/ }, () => ({
          path: 'fixture-api',
          namespace: 'fixture'
        }));
        builder.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
          contents: 'export const sessionApi = globalThis.__historyFixtureApi;'
        }));
        builder.onLoad({ filter: /\.svelte\.ts$/ }, async ({ path }) => {
          const js = await transform(await readFile(path, 'utf8'), {
            loader: 'ts'
          });
          return {
            contents: compileModule(js.code, {
              filename: path,
              generate: 'client'
            }).js.code,
            loader: 'js'
          };
        });
      }
    }
  ]
});
const transport = {};
globalThis.__historyFixtureApi = transport;
const { WorkflowStore, invalidationBus } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
delete globalThis.__historyFixtureApi;
function gate() {
  let resolve, reject;
  const promise = new Promise((a, b) => {
    resolve = a;
    reject = b;
  });
  return { promise, resolve, reject };
}
function snapshot() {
  return {
    session_id: 'a',
    stages: [
      {
        key: 'transcribe',
        selection_revision: 2,
        selected_artifact_id: 'latest',
        job_id: 'job',
        artifact_history_total: 2,
        artifact_history_has_more: true,
        artifact_history_next_before_version: 2,
        artifacts: [{ id: 'latest', version: 2 }]
      }
    ]
  };
}
function page() {
  return {
    revision: 2,
    selected_artifact_id: 'latest',
    total: 2,
    items: [{ id: 'older', version: 1 }],
    has_more: false,
    next_before_version: null
  };
}
for (const overlap of ['progress', 'reload']) {
  const store = new WorkflowStore('a'),
    pending = gate();
  store.replace(snapshot());
  transport.stageArtifacts = () => pending.promise;
  const loading = store.loadStageHistory('transcribe');
  const next = snapshot();
  next.stages[0].progress = 42;
  if (overlap === 'progress') {
    const disconnect = store.connect();
    invalidationBus.publish({
      resources: ['jobs'],
      session_ids: ['a'],
      job_ids: ['job'],
      events: [
        { session_id: 'a', job_id: 'job', progress: 42, status: 'running' }
      ]
    });
    disconnect();
  } else {
    transport.workflow = async () => next;
    await store.refresh();
  }
  pending.resolve(page());
  await loading;
  assert.deepEqual(
    store.snapshot.stages[0].artifacts.map((x) => x.id),
    ['latest', 'older']
  );
  assert.equal(store.snapshot.stages[0].progress, 42);
  store.replace(snapshot());
  assert.equal(
    store.snapshot.stages[0].artifacts.length,
    2,
    'loaded history survives unchanged reload'
  );
  assert.equal(
    store.snapshot.stages[0].artifact_history_next_before_version,
    null
  );
  assert.equal(store.historyLoading.transcribe, false);
  if (overlap === 'reload') {
    const changed = snapshot();
    changed.stages[0].artifact_history_total = 1;
    store.replace(changed);
    assert.equal(
      store.snapshot.stages[0].artifacts.length,
      1,
      'history mutation discards stale pages'
    );
  }
}
for (const change of ['selection', 'session', 'server-revision', 'failure']) {
  const store = new WorkflowStore('a'),
    pending = gate();
  store.replace(snapshot());
  transport.stageArtifacts = () => pending.promise;
  const loading = store.loadStageHistory('transcribe');
  if (change === 'session') {
    store.retarget('b');
    store.replace({ ...snapshot(), session_id: 'b' });
  }
  if (change === 'selection' || change === 'failure') {
    const next = snapshot();
    next.stages[0].selection_revision = 3;
    store.replace(next);
  }
  if (change === 'failure') pending.reject(new Error('obsolete failure'));
  else {
    const response = page();
    if (change === 'server-revision') response.revision = 3;
    pending.resolve(response);
  }
  await loading;
  assert.equal(store.snapshot.stages[0].artifacts.length, 1);
}
{
  const store = new WorkflowStore('a');
  store.replace(snapshot());
  transport.stageArtifacts = async () => {
    throw new Error('current page unavailable');
  };
  await assert.rejects(
    store.loadStageHistory('transcribe'),
    /current page unavailable/
  );
  assert.equal(store.historyLoading.transcribe, false);
}
console.log('workflow-history: 7 current-store/selection/session cases passed');
