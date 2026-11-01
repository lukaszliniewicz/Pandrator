import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { build } from 'esbuild';

const bundle = await build({
  stdin: {
    contents: `export * from './src/lib/voice-library-api';
      export * from './src/lib/voice-reference-api';
      export * from './src/lib/browser-identity';
      export { voiceApi, speechServiceApi } from './src/lib/admin-api';
      export { calls, responses } from './src/lib/api';`,
    resolveDir: process.cwd()
  },
  bundle: true,
  write: false,
  format: 'esm',
  plugins: [
    {
      name: 'record-library-transport',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/domain-api$/ }, () => ({
          path: 'domain-api',
          namespace: 'test-domain'
        }));
        builder.onLoad({ filter: /.*/, namespace: 'test-domain' }, () => ({
          contents: 'export const artifactApi = {}, jobApi = {};',
          loader: 'js'
        }));
        builder.onResolve({ filter: /(?:^\.\/api$|\/src\/lib\/api$)/ }, () => ({
          path: 'library-api',
          namespace: 'test-transport'
        }));
        builder.onLoad({ filter: /.*/, namespace: 'test-transport' }, () => ({
          contents: `export const calls = [], responses = [];
          export function apiJson(path, options) {
            calls.push({ path, options });
            return Promise.resolve(responses.shift() ?? { items: [] });
          }
          export function typedApiJson(path, method, options) {
            calls.push({ path, method, options });
            return Promise.resolve({ items: [] });
          }`,
          loader: 'js'
        }));
      }
    }
  ]
});

const library = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const api = library.voiceLibraryApi;
const roots = { workspaceId: 'team one', sessionId: 'project/one' };
const url = (call) => new URL(call.path, 'http://disposable.invalid');

test('Google creation and audition constrain workspace without changing their bodies', async () => {
  library.calls.length = 0;
  const design = {
    service_id: 'gemini',
    model: 'gemini-3.8-flash-tts',
    name: 'Workshop voice',
    prompt: 'Calm and clear'
  };
  const audition = {
    text: 'Workshop audition',
    model: 'voice-design',
    voice: ''
  };
  await library.voiceApi.designGoogle(
    design,
    'google-design-attempt',
    'team one'
  );
  await library.speechServiceApi.preview('audio_cpp', audition, 'team one');
  assert.equal(library.calls.length, 2);
  assert.equal(library.calls[0].path, '/api/v1/voices/design/google');
  assert.equal(
    library.calls[1].path,
    '/api/v1/services/tts/{serviceId}/preview'
  );
  assert.equal(library.calls[0].options.body, design);
  assert.equal(library.calls[1].options.body, audition);
  assert.equal(
    library.calls[0].options.headers['Idempotency-Key'],
    'google-design-attempt'
  );
  for (const call of library.calls)
    assert.deepEqual(call.options.query, { workspace_id: 'team one' });
  await library.voiceApi.designGoogle(design, 'legacy-attempt');
  await library.speechServiceApi.preview('audio_cpp', audition);
  assert.equal(library.calls[2].options.query, undefined);
  assert.equal(library.calls[3].options.query, undefined);
});

test('provider publication and removal capture workspace and revision in one request', async () => {
  library.calls.length = 0;
  await library.voiceApi.publish('voice/one', 'xtts', 7, 'team one');
  await library.voiceApi.unpublish('voice/one', 'xtts', 8, 'team one');
  assert.equal(library.calls.length, 2);
  for (const [index, call] of library.calls.entries()) {
    assert.equal(call.path, '/api/v1/voices/{voiceId}/providers/{serviceId}');
    assert.equal(call.method, index === 0 ? 'post' : 'delete');
    assert.deepEqual(call.options.path, {
      voiceId: 'voice/one',
      serviceId: 'xtts'
    });
    assert.deepEqual(call.options.query, { workspace_id: 'team one' });
    assert.equal(call.options.headers['If-Match'], `"${7 + index}"`);
    assert.equal(call.options.body, undefined);
  }
  await library.voiceApi.publish('voice', 'xtts', 1);
  await library.voiceApi.unpublish('voice', 'xtts', 2);
  assert.equal(library.calls[2].options.query, undefined);
  assert.equal(library.calls[3].options.query, undefined);
});

test('library search and collections retain explicit roots without extra calls', async () => {
  library.calls.length = 0;
  await api.query({ kind: 'provider', query: 'voice', limit: 5 }, roots);
  await api.collections(roots);
  assert.equal(library.calls.length, 2);
  for (const call of library.calls) {
    assert.equal(url(call).searchParams.get('workspace_id'), roots.workspaceId);
    assert.equal(url(call).searchParams.get('session_id'), roots.sessionId);
    assert.equal(call.options, undefined);
  }
  await api.collections();
  assert.equal(library.calls.at(-1).path, '/voice-collections');
});

test('in-flight library sharing isolates accounts, workspaces and projects', async () => {
  library.calls.length = 0;
  library.setBrowserIdentity('installation', 'alice', 1);
  const one = api.query({ limit: 5, query: 'voice' }, roots);
  assert.equal(one, api.query({ query: 'voice', limit: 5 }, roots));
  const otherWorkspace = api.query(
    { limit: 5, query: 'voice' },
    { workspaceId: 'other' }
  );
  const otherProject = api.query(
    { limit: 5, query: 'voice' },
    { sessionId: 'other' }
  );
  assert.notEqual(one, otherWorkspace);
  assert.notEqual(one, otherProject);
  library.setBrowserIdentity('installation', 'bob', 1);
  const otherAccount = api.query({ limit: 5, query: 'voice' }, roots);
  assert.notEqual(one, otherAccount);
  assert.equal(library.calls.length, 4);
  await Promise.all([one, otherWorkspace, otherProject, otherAccount]);
  const later = api.query({ limit: 5, query: 'voice' }, roots);
  assert.notEqual(later, otherAccount);
  await later;
  assert.equal(library.calls.length, 5);
});

test('managed voice lookup retains roots on its bounded fallback', async () => {
  library.calls.length = 0;
  library.responses.push(
    { items: [{ id: 'different' }], next_cursor: 'next' },
    { items: [{ id: 'target' }], next_cursor: null }
  );
  assert.equal((await api.managedVoice('target', roots)).id, 'target');
  assert.equal(library.calls.length, 2);
  for (const call of library.calls) {
    assert.equal(url(call).searchParams.get('workspace_id'), roots.workspaceId);
    assert.equal(url(call).searchParams.get('session_id'), roots.sessionId);
  }
  assert.equal(url(library.calls[1]).searchParams.get('query'), 'target');
});

test('collection and provider writes use authoritative workspace query and existing bodies', async () => {
  library.calls.length = 0;
  const collection = { id: 'group/one', revision: 3 };
  const reference = {
    kind: 'provider',
    service_id: 'team',
    model: 'model',
    voice: 'voice'
  };
  await api.createCollection('Team narrators', 'resolved team');
  await api.membership(collection, reference, true, 'resolved team');
  await api.updateCollection(
    collection,
    { name: 'Team cast' },
    'resolved team'
  );
  await api.update(
    { kind: 'provider', reference, revision: 2 },
    { name: 'Alice' },
    'resolved team'
  );
  assert.equal(library.calls.length, 4);
  assert.equal(
    url(library.calls[1]).pathname,
    '/voice-collections/group%2Fone'
  );
  assert.equal(url(library.calls[3]).pathname, '/voice-catalog/metadata');
  for (const call of library.calls) {
    assert.equal(url(call).searchParams.get('workspace_id'), 'resolved team');
    assert.equal(url(call).searchParams.has('session_id'), false);
    assert.equal('workspace_id' in JSON.parse(call.options.body), false);
  }
  assert.deepEqual(JSON.parse(library.calls[1].options.body), {
    expected_revision: 3,
    add_members: [reference]
  });
  assert.deepEqual(JSON.parse(library.calls[3].options.body), {
    reference,
    expected_revision: 2,
    changes: { name: 'Alice' }
  });
});

test('voice-record commands keep workspace in query and revisions in headers', async () => {
  library.calls.length = 0;
  await library.voiceApi.create({ name: 'Team reference' }, 'resolved team');
  await library.voiceApi.update(
    'voice/one',
    3,
    { name: 'Team cast' },
    'resolved team'
  );
  await library.voiceApi.delete('voice/one', 3, 'resolved team');
  await library.voiceApi.samples('voice/one', 'resolved team');
  await library.voiceApi.deleteSample(
    'voice/one',
    'sample/one',
    3,
    'resolved team'
  );
  assert.equal(library.calls.length, 5);
  for (const call of library.calls) {
    assert.deepEqual(call.options.query, { workspace_id: 'resolved team' });
    assert.equal('workspace_id' in (call.options.body ?? {}), false);
  }
  assert.deepEqual(
    library.calls.map((call) => call.method),
    ['post', 'patch', 'delete', 'get', 'delete']
  );
  assert.deepEqual(library.calls[0].options.body, { name: 'Team reference' });
  assert.deepEqual(library.calls[1].options.path, { voiceId: 'voice/one' });
  assert.equal(library.calls[1].options.headers['If-Match'], '"3"');
  assert.equal(library.calls[4].options.headers['If-Match'], '"3"');
  await library.voiceApi.samples('voice/one');
  assert.equal(library.calls.at(-1).options.query, undefined);
});

test('voice transcription and review keep the workspace query separate from settings', async () => {
  library.calls.length = 0;
  await library.voiceApi.transcribeSample(
    'voice/one',
    'sample/one',
    { stt_engine: 'whisper' },
    'resolved team'
  );
  await library.voiceApi.reviewTranscript(
    'voice/one',
    'sample/one',
    { transcript: 'Reviewed words', expected_voice_revision: 3 },
    'resolved team'
  );
  assert.equal(library.calls.length, 2);
  for (const call of library.calls) {
    assert.deepEqual(call.options.query, { workspace_id: 'resolved team' });
    assert.equal('workspace_id' in call.options.body, false);
    assert.equal('session_id' in call.options.body, false);
  }
  assert.deepEqual(library.calls[0].options.body, { stt_engine: 'whisper' });
  assert.deepEqual(library.calls[1].options.body, {
    transcript: 'Reviewed words',
    expected_voice_revision: 3
  });
  await library.voiceApi.transcribeSample('voice/one', 'sample/one', {});
  await library.voiceApi.reviewTranscript('voice/one', 'sample/one', {
    transcript: 'Legacy omission',
    expected_voice_revision: 3
  });
  assert.equal(library.calls[2].options.query, undefined);
  assert.equal(library.calls[3].options.query, undefined);
});

test('normalization commands constrain the workspace while preserving multipart and preview bodies', async () => {
  library.calls.length = 0;
  const recording = new FormData();
  const preview = {
    artifact_id: 'preview',
    transcript: 'Reviewed words',
    expected_voice_revision: 3
  };
  await library.voiceApi.uploadSample(
    'voice/one',
    3,
    recording,
    'resolved team'
  );
  await library.voiceApi.replaceSample(
    'voice/one',
    'sample/one',
    3,
    recording,
    'resolved team'
  );
  await library.voiceApi.promoteDesignPreview(
    'voice/one',
    3,
    preview,
    'resolved team'
  );
  assert.equal(library.calls.length, 3);
  for (const call of library.calls) {
    assert.deepEqual(call.options.query, { workspace_id: 'resolved team' });
    assert.equal(call.options.headers['If-Match'], '"3"');
  }
  assert.equal(library.calls[0].options.body, recording);
  assert.equal(library.calls[1].options.body, recording);
  assert.deepEqual(library.calls[2].options.body, preview);
  await library.voiceApi.uploadSample('voice/one', 3, recording);
  assert.equal(library.calls.at(-1).options.query, undefined);
});

test('reference file import and preparation share the explicit namespace and retry identity', async () => {
  library.calls.length = 0;
  const identities = [];
  const attempt = {
    headersFor(path, body) {
      identities.push({ path, body });
      return { 'Idempotency-Key': 'scoped-reference-key' };
    }
  };
  const file = { name: 'reference clip.wav', size: 32, lastModified: 1 };
  const body = {
    artifact_id: 'imported',
    expected_voice_revision: 3,
    language: 'en',
    transcript_reviewed: false
  };
  await library.importReferenceAudio(file, attempt, 'team & one');
  await library.prepareReference('voice/one', body, attempt, 'team & one');
  assert.equal(library.calls.length, 2);
  for (const [index, call] of library.calls.entries()) {
    assert.equal(url(call).searchParams.get('workspace_id'), 'team & one');
    assert.equal(identities[index].path, call.path);
    assert.equal(
      call.options.headers['Idempotency-Key'],
      'scoped-reference-key'
    );
  }
  assert.equal(library.calls[0].options.body, file);
  assert.equal(
    library.calls[0].options.headers['X-Audio-Filename'],
    'reference%20clip.wav'
  );
  assert.deepEqual(JSON.parse(library.calls[1].options.body), body);
  assert.equal(
    library.calls[1].options.headers['Content-Type'],
    'application/json'
  );
  assert.equal(
    url(library.calls[1]).pathname,
    '/voices/voice%2Fone/samples/from-artifact'
  );
  await library.importReferenceAudio(file, attempt);
  await library.prepareReference('voice/one', body, attempt);
  assert.equal(library.calls[2].path, '/voices/reference-audio/import');
  assert.equal(
    library.calls[3].path,
    '/voices/voice%2Fone/samples/from-artifact'
  );
});
