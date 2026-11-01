import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/run-metrics.ts'],
  bundle: true,
  write: false,
  format: 'esm'
});
const { apiTimestamp, estimatedRemaining, formatRunCost } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('naive API timestamps are UTC and aware offsets retain their meaning', () => {
  const utc = Date.parse('2026-10-20T12:55:29Z');
  assert.equal(apiTimestamp('2026-10-20T12:55:29'), utc);
  assert.equal(apiTimestamp('2026-10-20T14:55:29+02:00'), utc);
  assert.equal(apiTimestamp('2026-10-20T14:55:29+0200'), utc);
  assert.ok(Number.isNaN(apiTimestamp(null)));
  assert.ok(Number.isNaN(apiTimestamp('invalid')));
});

test('ETA waits for useful progress, remains approximate and handles the final phase', () => {
  for (const progress of [null, undefined, 0, -1, NaN, 0.001])
    assert.equal(estimatedRemaining(120, progress), 'Estimating…');
  assert.equal(estimatedRemaining(2, 0.5), 'Estimating…');
  assert.equal(estimatedRemaining(null, 0.5), 'Estimating…');
  assert.equal(estimatedRemaining(120, 0.25), 'About 6m');
  assert.equal(estimatedRemaining(120, 0.9), 'Under a minute');
  assert.equal(estimatedRemaining(120, 1), 'Finishing…');
});

test('missing prices stay distinct from a known zero price', () => {
  assert.equal(formatRunCost(null), 'not metered');
  assert.equal(formatRunCost(0), '$0.00');
  assert.equal(formatRunCost(0.0025), '$0.002500');
});
