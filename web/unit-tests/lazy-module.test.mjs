import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/lazy-module.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { loadLazyModule } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('one transient module fetch recovers without reloading the page', async () => {
  let attempts = 0;
  const component = {};
  const loaded = await loadLazyModule(async () => {
    if (++attempts === 1)
      throw new TypeError('Failed to fetch dynamically imported module');
    return component;
  });
  assert.equal(loaded, component);
  assert.equal(attempts, 2);
});

test('persistent fetch failure stops after the recovery attempt', async () => {
  let attempts = 0;
  await assert.rejects(
    loadLazyModule(async () => {
      attempts++;
      throw new TypeError('error loading dynamically imported module');
    }),
    TypeError
  );
  assert.equal(attempts, 2);
});

test('module initialization errors are surfaced without re-execution', async () => {
  let attempts = 0;
  await assert.rejects(
    loadLazyModule(async () => {
      attempts++;
      throw new ReferenceError('Initialization failed');
    }),
    ReferenceError
  );
  assert.equal(attempts, 1);
});
