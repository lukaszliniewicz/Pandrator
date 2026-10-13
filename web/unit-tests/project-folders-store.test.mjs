import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { build, transformSync } from 'esbuild';
import { compileModule } from 'svelte/compiler';

const bundle = await build({
  entryPoints: ['src/lib/project-folders-store.svelte.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser',
  plugins: [
    {
      name: 'folder-store-fixture',
      setup(builder) {
        builder.onResolve({ filter: /^\.\/api$/ }, () => ({
          path: 'api',
          namespace: 'folder-fixture'
        }));
        builder.onLoad({ filter: /.*/, namespace: 'folder-fixture' }, () => ({
          contents:
            'export const apiJson = (...args) => globalThis.folderFixture(...args);',
          loader: 'js'
        }));
        builder.onLoad({ filter: /\.svelte\.ts$/ }, (args) => ({
          contents: compileModule(
            transformSync(readFileSync(args.path, 'utf8'), { loader: 'ts' })
              .code,
            { filename: args.path, generate: 'client' }
          ).js.code,
          loader: 'js'
        }));
      }
    }
  ]
});
const { ProjectFoldersStore } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const empty = { items: [], memberships: [] };
const folder = (id, revision = 1) => ({
  id,
  name: id,
  parent_id: null,
  revision
});

test('a late folder read cannot replace the snapshot returned by a committed mutation', async () => {
  let release;
  const late = new Promise((resolve) => {
    release = resolve;
  });
  const saved = {
    items: [folder('course')],
    memberships: [],
    folder: folder('course')
  };
  globalThis.folderFixture = (_path, options) =>
    options ? Promise.resolve(saved) : late;
  const store = new ProjectFoldersStore();
  const loading = store.load();
  await store.create('course', null);
  release(empty);
  await loading;
  assert.equal(store.value.items[0].id, 'course');
  assert.equal(store.loading, false);
});

test('retrying an unchanged creation after a lost response uses the same idempotency key', async () => {
  const writes = [];
  globalThis.folderFixture = async (_path, options) => {
    if (!options) return empty;
    writes.push(options);
    if (writes.length === 1) throw new Error('Response lost');
    return { ...empty, items: [folder('course')], folder: folder('course') };
  };
  const store = new ProjectFoldersStore();
  await assert.rejects(store.create('course', null), /Response lost/);
  await store.create('course', null);
  assert.equal(
    writes[0].headers['Idempotency-Key'],
    writes[1].headers['Idempotency-Key']
  );
});

test('membership changes fence both removed and added folders, and conflict recovery reloads saved state', async () => {
  const first = {
    items: [folder('old', 3), folder('new', 7)],
    memberships: [{ folder_id: 'old', project_id: 'source', position: 0 }]
  };
  const latest = { ...first, items: [folder('old', 4), folder('new', 8)] };
  const writes = [];
  let reads = 0;
  globalThis.folderFixture = async (_path, options) => {
    if (!options) return ++reads === 1 ? first : latest;
    writes.push(JSON.parse(options.body));
    throw new Error('Folder changed');
  };
  const store = new ProjectFoldersStore();
  await store.load();
  await assert.rejects(store.assign('source', ['new']), /Folder changed/);
  assert.deepEqual(writes[0], {
    folder_ids: ['new'],
    expected_revisions: { old: 3, new: 7 }
  });
  assert.equal(store.value.items[0].revision, 4);
  assert.equal(store.value.memberships[0].folder_id, 'old');
});

test('creating a folder in an open assignment retains the original membership fences', async () => {
  const original = {
    items: [folder('old', 2)],
    memberships: [{ folder_id: 'old', project_id: 'source', position: 0 }]
  };
  const created = folder('new');
  const latest = { items: [folder('old', 3), created], memberships: [] };
  let assignment;
  globalThis.folderFixture = async (path, options) => {
    if (!options) return original;
    if (path === '/project-folders') return { ...latest, folder: created };
    assignment = JSON.parse(options.body);
    return latest;
  };
  const store = new ProjectFoldersStore();
  await store.load();
  const baseline = store.value;
  await store.create('new', null);
  await store.assign('source', ['new'], {
    ...baseline,
    items: [...baseline.items, created]
  });
  assert.deepEqual(assignment.expected_revisions, { old: 2, new: 1 });
});
