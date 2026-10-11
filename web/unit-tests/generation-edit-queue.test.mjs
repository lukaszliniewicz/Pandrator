import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/generation-edit-queue.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { GenerationEditQueue } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('a speech edit waits for wording to save and resolves copied segment IDs', async () => {
  const queue = new GenerationEditQueue();
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });
  const save = queue.save('original', async () => {
    await gate;
    return { id: 'copied', revision: 2 };
  });
  let completed = false;
  const ids = queue.settledIds(['original']).then((value) => {
    completed = true;
    return value;
  });
  await Promise.resolve();
  assert.equal(completed, false);
  release();
  await save;
  assert.deepEqual(await ids, ['copied']);
});

test('a failed wording save prevents opening against old text until its successful retry', async () => {
  const queue = new GenerationEditQueue();
  await assert.rejects(
    queue.save('original', async () => {
      throw new Error('Save failed');
    }),
    /Save failed/
  );
  await assert.rejects(queue.settledIds(['original']), /Save failed/);
  await queue.save('original', async () => ({ id: 'copied', revision: 2 }));
  assert.deepEqual(await queue.settledIds(['original']), ['copied']);
});
