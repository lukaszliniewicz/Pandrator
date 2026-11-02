import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/subtitle-timing.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node'
});
const { findCueTimingOverlaps } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('nested cues flag both affected sides without changing source order or timing', () => {
  const cues = [
    { start_ms: 800, end_ms: 1100, speaker: 'Bob' },
    { start_ms: 0, end_ms: 2000, speaker: 'Alice' },
    { start_ms: 300, end_ms: 500, speaker: 'Alice' }
  ];
  const before = JSON.stringify(cues);
  const overlaps = findCueTimingOverlaps(cues);
  assert.deepEqual([...overlaps.keys()].sort(), [0, 1, 2]);
  assert.deepEqual(overlaps.get(0), {
    overlap_ms: 300,
    different_speakers: true
  });
  assert.deepEqual(overlaps.get(2), {
    overlap_ms: 200,
    different_speakers: false
  });
  assert.equal(JSON.stringify(cues), before);
});

test('touching boundaries, isolated cues and invalid spans do not produce overlap warnings', () => {
  assert.equal(
    findCueTimingOverlaps([
      { start_ms: 0, end_ms: 100 },
      { start_ms: 100, end_ms: 200 },
      { start_ms: 150, end_ms: 150 },
      { start_ms: Number.NaN, end_ms: 500 },
      { start_ms: 500, end_ms: 600 }
    ]).size,
    0
  );
});

test('equal starts and unknown speakers remain warnings without assuming different voices', () => {
  const overlaps = findCueTimingOverlaps([
    { start_ms: 0, end_ms: 100, speaker: ' Alice ' },
    { start_ms: 0, end_ms: 300, speaker: 'alice' },
    { start_ms: 200, end_ms: 400 }
  ]);
  assert.equal(overlaps.size, 3);
  assert.equal(overlaps.get(0).different_speakers, false);
  assert.equal(overlaps.get(2).different_speakers, false);
});
