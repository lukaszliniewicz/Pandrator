import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/browser-identity.ts'],
  bundle: true,
  write: false,
  format: 'esm'
});
const identity = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
test('private browser caches are isolated across accounts, installations, workspaces and password epochs', () => {
  let resets = 0;
  const unsubscribe = identity.onBrowserIdentityReset(() => {
    resets += 1;
  });
  identity.setBrowserIdentity('installation-a', 'alice', 1);
  const first = identity.browserStorageKey('draft', 'project-a', 'team-a');
  identity.setBrowserIdentity('installation-a', 'alice', 1);
  assert.equal(resets, 1);
  assert.equal(
    identity.browserStorageKey('draft', 'project-a', 'team-a'),
    first
  );
  assert.notEqual(
    identity.browserStorageKey('draft', 'project-a', 'team-b'),
    first
  );
  identity.setBrowserIdentity('installation-a', 'bob', 1);
  assert.notEqual(
    identity.browserStorageKey('draft', 'project-a', 'team-a'),
    first
  );
  identity.setBrowserIdentity('installation-a', 'alice', 2);
  assert.notEqual(
    identity.browserStorageKey('draft', 'project-a', 'team-a'),
    first
  );
  identity.setBrowserIdentity('installation-b', 'alice', 1);
  assert.notEqual(
    identity.browserStorageKey('draft', 'project-a', 'team-a'),
    first
  );
  assert.equal(resets, 4);
  unsubscribe();
  identity.setBrowserIdentity('installation-b', '', 0);
  assert.equal(resets, 4);
});
