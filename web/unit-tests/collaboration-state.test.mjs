import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

const bundle = await build({
  entryPoints: ['src/lib/app-state.svelte.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  plugins: [
    {
      name: 'collaboration-state-fixture',
      setup(builder) {
        builder.onResolve(
          { filter: /^\.\/(api|domain-api|workspace-api)$/ },
          (args) => ({ path: args.path, namespace: 'state-fixture' })
        );
        builder.onLoad(
          { filter: /.*/, namespace: 'state-fixture' },
          (args) => ({
            loader: 'js',
            contents:
              args.path === './api'
                ? 'export const exchangeBootstrapToken = async () => {}; export const setCsrfToken = () => {}; export const apiJson = () => { throw new Error("Unexpected direct API request in state fixture"); };'
                : args.path === './workspace-api'
                  ? 'export const workspaceApi = { list: (...args) => globalThis.stateFixture.workspaces(...args) };'
                  : 'export const appApi = new Proxy({}, {get: (_, key) => (...args) => globalThis.stateFixture[key](...args)});'
          })
        );
        builder.onLoad({ filter: /\.svelte\.ts$/ }, (args) => ({
          loader: 'js',
          contents: compileModule(
            transformSync(readFileSync(args.path, 'utf8'), { loader: 'ts' })
              .code,
            { filename: args.path, generate: 'client' }
          ).js.code
        }));
      }
    }
  ]
});
let imports = 0;
const client = async () =>
  (
    await import(
      `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text + `\n// state case ${imports++}`).toString('base64')}`
    )
  ).appState;
const tick = () => new Promise((resolve) => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
}
function identity(user, enabled = true) {
  return {
    authenticated: true,
    instance_id: 'test-installation',
    csrf_token: 'synthetic',
    installation: {
      mode: enabled ? 'collaboration' : 'solo',
      collaboration_available: true,
      collaboration_enabled: enabled,
      revision: 1
    },
    principal: {
      user_id: user,
      auth_revision: 1,
      role: 'user',
      display_name: user,
      scopes: ['app.read', 'app.write']
    }
  };
}
const workspace = (id, kind = 'team') => ({
  id,
  kind,
  state: 'active',
  role: kind === 'personal' ? 'owner' : 'member',
  allowed_actions: ['read', 'edit'],
  name: id
});
const session = (id, workspaceId) => ({
  id,
  workspace_id: workspaceId,
  name: id,
  updated_at: '2030-01-01T00:00:00Z'
});
const snapshot = (sessions) => ({
  cursor: 1,
  sessions: { items: sessions },
  jobs: { items: [] },
  capabilities: {}
});
function environment(t, fixture, pathname = '/sessions', search = '') {
  const values = {
    stateFixture: fixture,
    location: { pathname, search, hash: '' },
    window: { setTimeout: () => 1, clearTimeout: () => {} },
    EventSource: class {
      close() {}
      addEventListener() {}
    },
    localStorage: { getItem: () => null, setItem: () => {} }
  };
  for (const [key, value] of Object.entries(values)) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { configurable: true, value });
    t.after(() =>
      previous
        ? Object.defineProperty(globalThis, key, previous)
        : delete globalThis[key]
    );
  }
}

test('Solo starts without a workspace request', async (t) => {
  let requests = 0;
  environment(t, {
    authStatus: async () => identity('alice', false),
    eventSnapshot: async () => snapshot([]),
    workspaces: async () => {
      requests += 1;
      throw new Error('Solo must not call this');
    }
  });
  const state = await client();
  await state.initialize();
  await tick();
  assert.equal(requests, 0);
  assert.equal(state.collaborationEnabled, false);
  assert.equal(state.error, '');
});

test('a deep-linked team editor waits for both the snapshot and workspace scope', async (t) => {
  const pending = deferred();
  environment(
    t,
    {
      authStatus: async () => identity('alice'),
      eventSnapshot: () => pending.promise,
      workspaces: async () => ({
        items: [workspace('home', 'personal'), workspace('team')]
      })
    },
    '/sessions/team-project'
  );
  const state = await client();
  await state.initialize();
  await tick();
  assert.equal(state.workspacesReady, false);
  pending.resolve(snapshot([session('team-project', 'team')]));
  await tick();
  assert.equal(state.workspacesReady, true);
  assert.equal(state.activeWorkspaceId, 'team');
});

test('responses from the previous account cannot replace the next account’s data', async (t) => {
  const oldSnapshot = deferred();
  const oldWorkspaces = deferred();
  let who = 'alice';
  let snapshots = 0;
  let workspaces = 0;
  environment(t, {
    authStatus: async () => identity(who),
    eventSnapshot: () =>
      ++snapshots === 1
        ? oldSnapshot.promise
        : Promise.resolve(snapshot([session('bob-private', 'bob-home')])),
    workspaces: () =>
      ++workspaces === 1
        ? oldWorkspaces.promise
        : Promise.resolve({ items: [workspace('bob-home', 'personal')] })
  });
  const state = await client();
  await state.initialize();
  await tick();
  who = 'bob';
  await state.refreshIdentity();
  oldSnapshot.resolve(snapshot([session('alice-private', 'alice-home')]));
  oldWorkspaces.resolve({ items: [workspace('alice-home', 'personal')] });
  await tick();
  assert.equal(state.principal.user_id, 'bob');
  assert.equal(state.activeWorkspaceId, 'bob-home');
  assert.deepEqual(
    state.sessions.map((item) => item.id),
    ['bob-private']
  );
  assert.deepEqual(
    state.workspaces.map((item) => item.id),
    ['bob-home']
  );
});

test('updating an active project retains its access metadata and other workspace records', async (t) => {
  environment(
    t,
    {
      authStatus: async () => identity('alice'),
      eventSnapshot: async () =>
        snapshot([
          session('team-project', 'team'),
          session('private-project', 'home')
        ]),
      workspaces: async () => ({
        items: [workspace('home', 'personal'), workspace('team')]
      })
    },
    '/sessions',
    '?workspace=team'
  );
  const state = await client();
  await state.initialize();
  await tick();
  state.upsertSession({
    id: 'team-project',
    name: 'Renamed',
    updated_at: '2030-01-02T00:00:00Z'
  });
  assert.equal(state.sessions[0].workspace_id, 'team');
  assert.equal(state.sessions[0].name, 'Renamed');
  assert.equal(state.workspaceForSession('private-project'), 'home');
});

test('a storage-wait event updates global jobs without waiting for another event or poll', async (t) => {
  const running = {
    id: 'export-job',
    session_id: 'video-project',
    kind: 'export.create',
    status: 'running',
    progress: 0.35,
    progress_detail: 'Preparing source media',
    created_at: '2030-01-01T00:00:00Z'
  };
  const held = {
    ...running,
    status: 'queued',
    error_code: 'storage_pressure',
    error_message: 'Waiting for output storage; inputs are retained.',
    progress_detail: 'Waiting for output storage; inputs are retained.',
    available_at: '2030-01-01T00:01:00Z'
  };
  let requests = 0;
  environment(t, {
    authStatus: async () => identity('alice', false),
    eventSnapshot: async () => ({
      ...snapshot([session('video-project', 'home')]),
      jobs: { items: [running] }
    }),
    jobs: async () => {
      requests += 1;
      return { items: [held] };
    }
  });
  const sources = [];
  Object.defineProperty(globalThis, 'EventSource', {
    configurable: true,
    value: class {
      listeners = new Map();
      constructor() {
        sources.push(this);
      }
      addEventListener(type, listener) {
        this.listeners.set(type, listener);
      }
      close() {}
    }
  });
  let flush;
  globalThis.window.setTimeout = (callback) => {
    flush = callback;
    return 1;
  };
  const state = await client();
  await state.initialize();
  await tick();
  assert.equal(state.jobs[0].status, 'running');
  assert.equal(requests, 0);
  const source = sources.at(-1);
  source.onopen();
  const receive = source.listeners.get('job.waiting_for_storage');
  assert.equal(typeof receive, 'function');
  receive({
    lastEventId: '2',
    data: JSON.stringify({
      job_id: held.id,
      session_id: held.session_id,
      job_kind: held.kind,
      status: held.status,
      progress: held.progress,
      detail: held.progress_detail,
      changed_entities: ['jobs']
    })
  });
  assert.equal(state.jobs[0].status, 'queued');
  assert.equal(requests, 0);
  flush();
  await tick();
  assert.equal(requests, 1);
  assert.deepEqual(state.jobs[0], held);
});
