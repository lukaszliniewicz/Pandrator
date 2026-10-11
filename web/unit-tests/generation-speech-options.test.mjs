import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath, URL } from 'node:url';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

const filename = 'generation-speech-options.svelte.ts';
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
      name: 'isolated-speech-options-api',
      setup(builder) {
        builder.onResolve(
          { filter: /^\.\/(domain-api|tts-catalogue-cache)$/ },
          (args) => ({ path: args.path, namespace: 'speech-options-mock' })
        );
        builder.onLoad(
          { filter: /.*/, namespace: 'speech-options-mock' },
          (args) => ({
            contents: args.path.includes('domain-api')
              ? 'export const sessionApi = {settings: (...args) => globalThis.speechOptionsFixture.settings(...args), discoverTts: () => {throw new Error("Discovery must not run")} };'
              : 'export const getTtsCompactCatalogue = (...args) => globalThis.speechOptionsFixture.catalogue(...args); export const getVoiceLibrary = (...args) => globalThis.speechOptionsFixture.voices(...args);',
            loader: 'js'
          })
        );
      }
    }
  ]
});
const { GenerationSpeechOptionsState } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

function setup() {
  const reads = [];
  let catalogues = 0,
    libraries = 0;
  globalThis.speechOptionsFixture = {
    settings: (sessionId, section) => {
      const task = deferred();
      reads.push({ sessionId, section, ...task });
      return task.promise;
    },
    catalogue: async () => {
      catalogues++;
      return {
        services: [{ id: 'vertex_ai', models: ['gemini-3.8-flash-tts'] }]
      };
    },
    voices: async () => {
      libraries++;
      return { items: [{ id: 'saved-voice' }] };
    }
  };
  let sessionId = 'project';
  return {
    state: new GenerationSpeechOptionsState(
      () => sessionId,
      (id) => String(id)
    ),
    reads,
    counts: () => ({ catalogues, libraries }),
    changeScope: () => {
      sessionId = 'other-project';
    }
  };
}

test('a mode refresh wins over an older supporting-options response without catalogue discovery', async () => {
  const fixture = setup();
  const initial = fixture.state.load();
  const changed = fixture.state.refreshSettings();
  fixture.reads[1].resolve({
    effective: { service: 'vertex_ai', casting_enabled: true }
  });
  await changed;
  fixture.reads[0].resolve({ effective: { casting_enabled: false } });
  await initial;
  assert.equal(fixture.state.settings.casting_enabled, true);
  assert.deepEqual(fixture.counts(), { catalogues: 1, libraries: 1 });
  assert.equal(fixture.state.voices[0].id, 'saved-voice');
});

test('rapid mode changes retain only the newest effective settings', async () => {
  const fixture = setup();
  const older = fixture.state.refreshSettings(),
    newer = fixture.state.refreshSettings();
  fixture.reads[1].resolve({ effective: { casting_enabled: false } });
  await newer;
  fixture.reads[0].resolve({ effective: { casting_enabled: true } });
  await older;
  assert.equal(fixture.state.settings.casting_enabled, false);
  assert.deepEqual(fixture.counts(), { catalogues: 0, libraries: 0 });
});

test('disposal and a changed project cannot receive a stale settings response', async () => {
  for (const end of ['dispose', 'scope']) {
    const fixture = setup();
    const pending = fixture.state.refreshSettings();
    if (end === 'dispose') fixture.state.dispose();
    else fixture.changeScope();
    fixture.reads[0].resolve({ effective: { casting_enabled: true } });
    await pending;
    assert.deepEqual(fixture.state.settings, {});
  }
});

test('simple preview loads settings and model support without the voice library or discovery', async () => {
  const fixture = setup();
  const initial = fixture.state.load({ editing: false });
  fixture.reads[0].resolve({
    effective: { service: 'vertex_ai', casting_enabled: true }
  });
  await initial;
  assert.deepEqual(fixture.counts(), { catalogues: 1, libraries: 0 });
  assert.equal(fixture.state.settings.casting_enabled, true);
  assert.deepEqual(fixture.state.voices, []);
  const advanced = fixture.state.load({ editing: true });
  fixture.reads[1].resolve({
    effective: { service: 'vertex_ai', casting_enabled: true }
  });
  await advanced;
  assert.equal(fixture.counts().libraries, 1);
  assert.equal(fixture.state.voices[0].id, 'saved-voice');
});
