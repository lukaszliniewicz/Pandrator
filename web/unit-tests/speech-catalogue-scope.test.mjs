import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  stdin: {
    contents: `export * from './src/lib/speech-catalogue-api';
      export * from './src/lib/tts-catalogue-cache';
      export * from './src/lib/browser-identity';
      export * from './src/lib/tts-provider-policy';`,
    resolveDir: process.cwd()
  },
  bundle: true,
  write: false,
  format: 'esm',
  plugins: [
    {
      name: 'record-transport',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/(api|domain-api)$/ }, ({ path }) => ({
          path,
          namespace: 'test-transport'
        }));
        builder.onLoad({ filter: /.*/, namespace: 'test-transport' }, () => ({
          contents: `export const calls = [];
          export function typedApiJson(path, method, options) {
            return Promise.resolve({ path, method, options });
          }
          export const sessionApi = new Proxy({}, { get(_, name) {
            return (...args) => { const reply = { name, args };
              calls.push(reply); return Promise.resolve(reply); };
          }});`,
          loader: 'js'
        }));
      }
    }
  ]
});
const speech = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('catalogue reads retain explicit roots and refresh uses POST only', async () => {
  const read = await speech.fetchTtsCatalogue(
    false,
    {
      workspaceId: 'team one',
      sessionId: 'project/one'
    },
    'compact'
  );
  assert.equal(read.method, 'get');
  assert.equal(read.path, '/api/v1/services/tts');
  assert.equal(read.options.query.get('workspace_id'), 'team one');
  assert.equal(read.options.query.get('session_id'), 'project/one');
  assert.equal(read.options.query.get('view'), 'compact');
  assert.equal(read.options.query.has('refresh'), false);
  const refreshed = await speech.fetchTtsCatalogue(true, {
    workspaceId: 'team one'
  });
  assert.equal(refreshed.method, 'post');
  assert.equal(refreshed.path, '/api/v1/services/tts/refresh');
  assert.equal(refreshed.options.query.get('workspace_id'), 'team one');
  assert.throws(
    () => speech.fetchTtsCatalogue(true, { sessionId: 'project/one' }),
    /catalogue workspace/
  );
  const detail = await speech.fetchTtsServiceDetail('audio_cpp', {
    sessionId: 'project/one',
    models: ['a', 'b']
  });
  assert.equal(detail.options.query.get('session_id'), 'project/one');
  assert.equal(detail.options.query.get('models'), 'a,b');
  const refreshDetail = await speech.fetchTtsServiceDetail('audio_cpp', {
    workspaceId: 'team one',
    refresh: true,
    models: 'a'
  });
  assert.equal(refreshDetail.method, 'post');
  assert.equal(refreshDetail.path, '/api/v1/services/tts/{serviceId}/refresh');
  assert.equal(refreshDetail.options.query.get('model'), 'a');
  assert.throws(
    () =>
      speech.fetchTtsServiceDetail('audio_cpp', {
        sessionId: 'project/one',
        refresh: true
      }),
    /catalogue workspace/
  );
});

test('in-flight deduplication never joins different accounts or workspace/project roots', async () => {
  speech.setBrowserIdentity('installation', 'alice', 1);
  const teamA = speech.getTtsCompactCatalogue(false, false, {
    workspaceId: 'team-a'
  });
  assert.equal(
    teamA,
    speech.getTtsCompactCatalogue(false, false, { workspaceId: 'team-a' })
  );
  assert.notEqual(
    teamA,
    speech.getTtsCompactCatalogue(false, false, { workspaceId: 'team-b' })
  );
  assert.notEqual(
    teamA,
    speech.getTtsCompactCatalogue(false, false, { sessionId: 'project-a' })
  );
  speech.setBrowserIdentity('installation', 'bob', 1);
  assert.notEqual(
    teamA,
    speech.getTtsCompactCatalogue(false, false, { workspaceId: 'team-a' })
  );
  const voicesA = speech.getVoiceLibrary(false, { workspaceId: 'team-a' });
  assert.equal(
    voicesA,
    speech.getVoiceLibrary(false, { workspaceId: 'team-a' })
  );
  assert.notEqual(
    voicesA,
    speech.getVoiceLibrary(false, { workspaceId: 'team-b' })
  );
  const selected = speech.getTtsServiceDetail('audio_cpp', {
    sessionId: 'project-a',
    models: 'a'
  });
  assert.equal(
    selected,
    speech.getTtsServiceDetail('audio_cpp', {
      sessionId: 'project-a',
      models: 'a'
    })
  );
  assert.notEqual(
    selected,
    speech.getTtsServiceDetail('audio_cpp', {
      sessionId: 'project-b',
      models: 'a'
    })
  );
  await teamA;
  assert.notEqual(
    teamA,
    speech.getTtsCompactCatalogue(false, false, { workspaceId: 'team-a' })
  );
});

test('saved service aliases and unchecked availability preserve selectable project controls', () => {
  const service = {
    id: 'team-approved-tts',
    name: 'Team approved speech',
    available: null
  };
  assert.equal(speech.serviceMatches(service, 'team_approved_tts'), true);
  assert.equal(speech.serviceMatches({ id: 'audio_cpp' }, 'audio.cpp'), true);
  assert.equal(speech.serviceMatches({ id: 'kobold_qwen' }, 'qwen3-tts'), true);
  assert.equal(speech.serviceMatches(service, 'other-team-tts'), false);
  assert.equal(speech.ttsServiceUsable(service), true);
  assert.equal(speech.ttsServiceUsable({ ...service, available: true }), true);
  assert.equal(
    speech.ttsServiceUsable({ ...service, available: false }),
    false
  );
  assert.equal(speech.ttsServiceUsable(undefined), false);
});
