import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/speech-word-edit.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const { applySpeechWordEdit } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('moving from annotated words to a textarea preserves Unicode selection and first input', () => {
  assert.deepEqual(applySpeechWordEdit('A😀B', { start: 1, end: 2 }), {
    value: 'A😀B',
    start: 1,
    end: 3
  });
  assert.deepEqual(
    applySpeechWordEdit('A😀B', {
      start: 2,
      end: 2,
      inputType: 'insertText',
      data: 'x'
    }),
    { value: 'A😀xB', start: 4, end: 4 }
  );
  assert.deepEqual(
    applySpeechWordEdit('A😀B', {
      start: 1,
      end: 2,
      inputType: 'insertFromPaste',
      data: 'Hello\nWorld'
    }),
    { value: 'AHello\nWorldB', start: 12, end: 12 }
  );
});

test('Backspace and Delete remove whole combined letters and emoji', () => {
  assert.equal(
    applySpeechWordEdit('Ae\u0301B', {
      start: 3,
      end: 3,
      inputType: 'deleteContentBackward'
    }).value,
    'AB'
  );
  assert.equal(
    applySpeechWordEdit('A👩🏽‍🚀B', {
      start: 1,
      end: 1,
      inputType: 'deleteContentForward'
    }).value,
    'AB'
  );
  assert.equal(
    applySpeechWordEdit('A😀B', { start: 1, end: 2, inputType: 'deleteByCut' })
      .value,
    'AB'
  );
});
