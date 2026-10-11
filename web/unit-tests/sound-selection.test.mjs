import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/sound-selection.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { soundAnchorForSelection, soundReturnUrl } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const select = (text, start, end = start, extra = {}) =>
  soundAnchorForSelection({
    item: { id: 'segment-1', text, ...extra },
    start,
    end
  });

test('a caret inside a word uses the whole word rather than its truncated prefix', () => {
  assert.deepEqual(select('The kettle is boiling.', 8), {
    kind: 'after_word',
    segment_id: 'segment-1',
    quote: 'kettle',
    occurrence: 1,
    start: 4,
    end: 10
  });
  assert.equal(select('The kettle is boiling.', 11).quote, 'kettle');
});

test('repeated words retain exact occurrence and Unicode code-point offsets', () => {
  const anchor = select('🔔 bell, bell rings.', 8, 12);
  assert.equal(anchor.quote, 'bell');
  assert.equal(anchor.start, 8);
  assert.equal(anchor.end, 12);
  const word = select('🔔 bell, bell rings.', 9);
  assert.equal(word.quote, 'bell');
  assert.equal(word.occurrence, 2);
  assert.equal(word.start, 8);
  assert.equal(word.end, 12);
});

test('anchors use optimized speech and preserve segment boundary intents', () => {
  assert.equal(
    select('Display text', 4, 4, { optimized_text: 'The bell rang.' }).quote,
    'The'
  );
  assert.deepEqual(select('A bell.', 0), {
    kind: 'before_segment',
    segment_id: 'segment-1'
  });
  assert.deepEqual(select('A bell.', 7), {
    kind: 'after_segment',
    segment_id: 'segment-1'
  });
  assert.equal(select('A bell.', 2, 6).quote, 'bell');
});

test('stale ranges and split graphemes fail before a cue request', () => {
  for (const [start, end] of [
    [-1, 2],
    [3, 2],
    [0, 999],
    [1.5, 2]
  ]) {
    assert.throws(() => select('bell rings', start, end), /selection changed/);
  }
  assert.throws(() => select('cafe\u0301 bell', 4), /complete characters/);
  assert.throws(() => select('🔔 bell', 1, 2), /Select a word/);
  assert.throws(() => select(`${'a'.repeat(501)} rings`, 0, 501), /up to 500/);
});

test('playground return links encode anchors and optional saved sound IDs', () => {
  const url = new URL(
    soundReturnUrl(
      'project/id',
      { kind: 'source_time', time_seconds: 2.5 },
      'saved/id'
    ),
    'http://localhost'
  );
  assert.equal(url.pathname, '/sessions/project%2Fid/generation');
  assert.deepEqual(JSON.parse(url.searchParams.get('sound_anchor')), {
    kind: 'source_time',
    time_seconds: 2.5
  });
  assert.equal(url.searchParams.get('sound_item'), 'saved/id');
});
