import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/speech-annotations.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { textareaSpeechRange } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const item = { id: 'segment', text: 'A😀B', optimized_text: null };
const textarea = (start, end, value = item.text) => ({
  value,
  selectionStart: start,
  selectionEnd: end,
  getBoundingClientRect: () => ({ left: 20, top: 30, bottom: 50 })
});

test('caret insertion is opt-in and uses codepoints after a surrogate pair', () => {
  const node = textarea(3, 3);
  assert.equal(textareaSpeechRange(node, item, 'speech'), null);
  const cursor = textareaSpeechRange(node, item, 'speech', true);
  assert.equal(cursor.kind, 'cursor');
  assert.equal(cursor.start, 2);
  assert.equal(cursor.end, 2);
  assert.equal(cursor.anchor, node);
});

test('phrase offsets remain half-open codepoints in both editing modes', () => {
  for (const allowCaret of [false, true]) {
    const range = textareaSpeechRange(
      textarea(1, 3),
      item,
      'speech',
      allowCaret
    );
    assert.equal(range.kind, 'phrase');
    assert.equal(range.start, 1);
    assert.equal(range.end, 2);
  }
});

test('unsaved words and differing subtitle text cannot become speech annotations', () => {
  assert.equal(
    textareaSpeechRange(
      textarea(1, 1, 'Different words'),
      item,
      'speech',
      true
    ),
    null
  );
  const optimized = { ...item, optimized_text: 'Speech override' };
  assert.equal(
    textareaSpeechRange(textarea(1, 1), optimized, 'display', true),
    null
  );
  const cursor = textareaSpeechRange(
    textarea(2, 2, optimized.optimized_text),
    optimized,
    'speech',
    true
  );
  assert.equal(cursor.start, 2);
  assert.equal(cursor.end, 2);
});
