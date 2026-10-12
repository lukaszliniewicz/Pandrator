import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFile } from 'node:fs/promises';
import { setImmediate } from 'node:timers';
import { test } from 'node:test';
import process from 'node:process';
import { build, transform } from 'esbuild';
import { compileModule } from 'svelte/compiler';

const bundle = await build({
  stdin: {
    contents:
      "export { GenerationSummaryStore } from './src/lib/generation-summary-store.svelte.ts'; export { invalidationBus } from './src/lib/invalidation.ts';",
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
      name: 'usage-fixtures',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/domain-api$/ }, () => ({
          path: 'api',
          namespace: 'fixture'
        }));
        builder.onLoad({ filter: /.*/, namespace: 'fixture' }, () => ({
          contents: 'export const generationApi = globalThis.__summaryUsageApi;'
        }));
        builder.onLoad({ filter: /\.svelte\.ts$/ }, async ({ path }) => {
          const code = await transform(await readFile(path, 'utf8'), {
            loader: 'ts'
          });
          return {
            contents: compileModule(code.code, {
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
globalThis.__summaryUsageApi = transport;
const { GenerationSummaryStore, invalidationBus } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text + '\n//# sourceURL=generation-summary-usage-fixture.js').toString('base64')}`
);
delete globalThis.__summaryUsageApi;
const settled = () => new Promise((resolve) => setImmediate(resolve));

test('progress refreshes compact cost totals without a reload, coalesces bursts and cleans up on navigation', async (t) => {
  const originalWindow = globalThis.window;
  let disconnect = () => {};
  const timers = new Map();
  let nextTimer = 0;
  globalThis.window = {
    setTimeout: (callback, delay) => {
      const id = ++nextTimer;
      timers.set(id, { callback, delay });
      return id;
    },
    clearTimeout: (id) => timers.delete(id)
  };
  t.after(() => {
    disconnect();
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
  });
  let calls = 0;
  let cost = 0.001;
  transport.summary = async (sessionId) => {
    calls++;
    assert.equal(sessionId, 'session');
    return {
      session_id: sessionId,
      total: 569,
      included_total: 569,
      plan_revision_id: 'plan',
      active_run: {
        id: 'run',
        job_id: 'job',
        status: 'running',
        progress: 0.2,
        progress_detail: 'Generating segment 114 of 569',
        usage: { total_cost_usd: cost, estimated: true }
      },
      assembly: null
    };
  };
  const store = new GenerationSummaryStore('session');
  disconnect = store.connect();
  await settled();
  assert.equal(store.value.active_run.usage.total_cost_usd, 0.001);
  const progress = (sessionId = 'session') =>
    invalidationBus.publish({
      resources: ['jobs'],
      session_ids: [sessionId],
      job_ids: ['job'],
      events: [
        {
          type: 'job.progress',
          session_id: sessionId,
          job_id: 'job',
          progress: 0.3
        }
      ]
    });
  progress('foreign-session');
  assert.equal(timers.size, 0);
  cost = 0.004;
  for (let index = 0; index < 20; index++) progress();
  assert.equal(timers.size, 1, 'progress bursts cause one compact read');
  assert.equal(calls, 1);
  const [id, timer] = [...timers][0];
  assert.equal(timer.delay, 1000);
  timers.delete(id);
  timer.callback();
  await settled();
  assert.equal(calls, 2);
  assert.equal(store.value.active_run.usage.total_cost_usd, 0.004);
  assert.equal(store.value.total, 569);
  progress();
  assert.equal(timers.size, 1);
  disconnect();
  assert.equal(timers.size, 0);
  progress();
  assert.equal(timers.size, 0);
  disconnect = store.connect();
  await settled();
  progress();
  assert.equal(timers.size, 1, 'reconnecting does not retain a cleared timer');
});
