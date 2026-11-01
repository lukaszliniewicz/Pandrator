import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/subtitle-review-index.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'node'
});
const { createSubtitleReviewIndex } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const same = (left, right) =>
  left === right ||
  Boolean(left.id && left.id === right.id) ||
  Boolean(left.draft_id && left.draft_id === right.draft_id);

test('row copies, duplicate IDs and draft identities keep first matching record', () => {
  const records = [
    { id: 'a' },
    { draft_id: 'x' },
    { id: 'b', draft_id: 'x' },
    {},
    { id: 'a' }
  ];
  const index = createSubtitleReviewIndex(records);
  for (const query of [
    ...records,
    { id: 'a' },
    { id: 'b', draft_id: 'x' },
    { id: 'unknown' },
    {}
  ]) {
    const expected = records.findIndex((record) => same(record, query));
    assert.equal(index.indexOf(query), expected);
    assert.equal(index.canonical(query), records[expected] ?? query);
  }
});

test('rebuilding after split, merge and removal resolves current editable objects', () => {
  const first = { id: 'first', text: 'Original' };
  const records = [first, { id: 'second' }];
  assert.equal(
    createSubtitleReviewIndex(records).canonical({ id: 'first' }),
    first
  );
  records.splice(0, 1, { draft_id: 'left' }, { draft_id: 'right' });
  let index = createSubtitleReviewIndex(records);
  assert.equal(index.indexOf({ id: 'first' }), -1);
  assert.equal(index.indexOf({ draft_id: 'right' }), 1);
  records.splice(0, 2, { draft_id: 'merged' });
  index = createSubtitleReviewIndex(records);
  assert.equal(index.indexOf({ draft_id: 'merged' }), 0);
  assert.equal(index.indexOf({ id: 'second' }), 1);
});

test('large review lookup reads identifiers in proportion to the number of records', () => {
  let reads = 0;
  const records = Array.from({ length: 5000 }, (_, i) => ({
    get id() {
      reads++;
      return `cue-${i}`;
    }
  }));
  const index = createSubtitleReviewIndex(records);
  records.forEach((_, i) => assert.equal(index.indexOf({ id: `cue-${i}` }), i));
  assert.ok(reads < 5000 * 8, `Unexpected repeated source scans: ${reads}`);
});
