import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath, URL } from 'node:url';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

const filename = 'tts-catalogue-state.svelte.ts';
const source = readFileSync(
  new URL(`../src/lib/${filename}`, import.meta.url),
  'utf8'
);
const compiled = compileModule(transformSync(source, { loader: 'ts' }).code, {
  filename,
  generate: 'client'
}).js.code;
const bundle = await build({
  stdin: {
    contents: compiled,
    resolveDir: fileURLToPath(new URL('../src/lib', import.meta.url)),
    sourcefile: filename
  },
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  plugins: [
    {
      name: 'isolated-catalogue',
      setup(builder) {
        builder.onResolve(
          { filter: /^\.\/(domain-api|tts-catalogue-cache)$/ },
          (args) => ({ path: args.path, namespace: 'catalogue-mock' })
        );
        builder.onLoad(
          { filter: /.*/, namespace: 'catalogue-mock' },
          (args) => ({
            contents: args.path.includes('domain-api')
              ? 'export const sessionApi = {discoverTts: () => {throw new Error("Unexpected discovery")} };'
              : 'export const getTtsCompactCatalogue = (...args) => globalThis.catalogueFixture.catalogue(...args); export const getVoiceLibrary = (...args) => globalThis.catalogueFixture.voices(...args); export const getTtsServiceDetail = (...args) => globalThis.catalogueFixture.detail(...args);',
            loader: 'js'
          })
        );
      }
    }
  ]
});
const { TtsCatalogueState } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function fixture() {
  const calls = { catalogue: [], voices: [], detail: [] };
  globalThis.catalogueFixture = Object.fromEntries(
    Object.keys(calls).map((name) => [
      name,
      (...args) => {
        const pending = deferred();
        calls[name].push({ args, ...pending });
        return pending.promise;
      }
    ])
  );
  return { state: new TtsCatalogueState(), calls };
}
const catalogue = {
  services: [
    {
      id: 'vertex_ai',
      name: 'Vertex AI',
      models: ['gemini-3.8-flash-tts'],
      available: null
    }
  ]
};

test('cached models become available while the saved voice library is still pending', async () => {
  const { state, calls } = fixture();
  const load = state.load();
  assert.deepEqual(calls.catalogue[0].args, [false, false]);
  calls.catalogue[0].resolve(catalogue);
  const receipt = await load;
  assert.equal(state.finishLoad(receipt), true);
  assert.equal(state.catalogue.services[0].id, 'vertex_ai');
  assert.equal(state.voicesLoading, true);
  assert.deepEqual(calls.detail, []);
  calls.voices[0].resolve({ items: [{ id: 'voice-a' }] });
  await calls.voices[0].promise;
  assert.equal(state.voicesLoading, false);
  assert.equal(state.voices[0].id, 'voice-a');
});

test('reopening after a pending voice load restarts it and ignores the old result', async () => {
  const { state, calls } = fixture();
  const load = state.load();
  calls.catalogue[0].resolve(catalogue);
  state.finishLoad(await load);
  state.invalidate();
  await state.load();
  assert.equal(calls.catalogue.length, 1);
  assert.equal(calls.voices.length, 2);
  calls.voices[0].resolve({ items: [{ id: 'old-voice' }] });
  await calls.voices[0].promise;
  assert.deepEqual(state.voices, []);
  calls.voices[1].resolve({ items: [{ id: 'new-voice' }] });
  await calls.voices[1].promise;
  assert.equal(state.voices[0].id, 'new-voice');
});

test('selected-provider checks coalesce and cannot repopulate an invalidated modal', async () => {
  const { state, calls } = fixture();
  state.catalogue = catalogue;
  const check = state.refreshService('vertex_ai');
  assert.equal(state.refreshService('vertex_ai'), check);
  assert.deepEqual(calls.detail[0].args, ['vertex_ai', { refresh: true }]);
  assert.deepEqual(state.checkingServices, ['vertex_ai']);
  state.invalidate();
  calls.detail[0].resolve({
    service: { ...catalogue.services[0], available: true }
  });
  await check;
  assert.equal(state.catalogue.services[0].available, null);
  assert.deepEqual(state.checkingServices, []);
  const fresh = state.refreshService('vertex_ai');
  calls.detail[1].resolve({
    service: { ...catalogue.services[0], available: true }
  });
  await fresh;
  assert.equal(state.catalogue.services[0].available, true);
});

test('a failed saved-voice request does not hide models or prevent a retry', async () => {
  const { state, calls } = fixture();
  const load = state.load();
  calls.catalogue[0].resolve(catalogue);
  state.finishLoad(await load);
  calls.voices[0].reject(new Error('Voice library unavailable'));
  await calls.voices[0].promise.catch(() => undefined);
  assert.equal(state.voiceError, 'Voice library unavailable');
  assert.equal(state.error, '');
  assert.equal(state.catalogue.services.length, 1);
  await state.load();
  calls.voices[1].resolve({ items: [] });
  await calls.voices[1].promise;
  assert.equal(state.voiceError, '');
});

test('a failed provider check keeps cached models and exposes a retryable error', async () => {
  const { state, calls } = fixture();
  state.catalogue = catalogue;
  const check = state.refreshService('vertex_ai');
  calls.detail[0].reject(new Error('Provider check timed out'));
  await check;
  assert.equal(state.serviceErrors.vertex_ai, 'Provider check timed out');
  assert.deepEqual(state.catalogue.services[0].models, [
    'gemini-3.8-flash-tts'
  ]);
  assert.deepEqual(state.checkingServices, []);
  const retry = state.refreshService('vertex_ai');
  assert.equal(state.serviceErrors.vertex_ai, '');
  calls.detail[1].resolve({
    service: { ...catalogue.services[0], available: true }
  });
  await retry;
  assert.equal(state.catalogue.services[0].available, true);
});

test('an explicit catalogue refresh clears superseded provider loading state', async () => {
  const { state, calls } = fixture();
  state.catalogue = catalogue;
  const oldCheck = state.refreshService('vertex_ai');
  const load = state.load(true);
  assert.deepEqual(calls.catalogue[0].args, [true, true]);
  assert.deepEqual(state.checkingServices, []);
  calls.catalogue[0].resolve({
    services: [{ ...catalogue.services[0], available: true }]
  });
  state.finishLoad(await load);
  calls.detail[0].resolve({
    service: { ...catalogue.services[0], available: false }
  });
  await oldCheck;
  assert.equal(state.catalogue.services[0].available, true);
  assert.deepEqual(state.checkingServices, []);
  calls.voices[0].resolve({ items: [] });
  await calls.voices[0].promise;
});
