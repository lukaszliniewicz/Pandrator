import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/api.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { ApiMutationAttempt } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const key = (attempt, target, body) =>
  attempt.headersFor(target, body)['Idempotency-Key'];

test('an unchanged mutation retry retains its key after the original body is mutated', () => {
  const attempt = new ApiMutationAttempt();
  const body = { ids: ['old'], settings: { voice: 'original' } };
  const original = key(attempt, '/session-a/start', body);
  assert.ok(original);
  body.ids[0] = 'edited';
  body.settings.voice = 'changed';
  assert.equal(
    key(attempt, '/session-a/start', {
      ids: ['old'],
      settings: { voice: 'original' }
    }),
    original
  );
  const changed = key(attempt, '/session-a/start', body);
  assert.notEqual(changed, original);
  assert.equal(key(attempt, '/session-a/start', body), changed);
});

test('different targets and independent attempts cannot replay one another', () => {
  const body = { operation: 'regenerate', ids: ['segment'] };
  const attempt = new ApiMutationAttempt();
  const first = key(attempt, '/session-a/start', body);
  assert.notEqual(key(attempt, '/session-b/start', body), first);
  assert.notEqual(
    key(new ApiMutationAttempt(), '/session-a/start', body),
    first
  );
});
