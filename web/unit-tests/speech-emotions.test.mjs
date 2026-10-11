import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

async function moduleFrom(entry) {
  const bundle = await build({
    entryPoints: [entry],
    bundle: true,
    write: false,
    format: 'esm',
    platform: 'browser'
  });
  return import(
    `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
  );
}
const { emotionChoices, emotionChoiceKind } = await moduleFrom(
  'src/lib/speech-emotions.ts'
);
const { deliverySupport } = await moduleFrom('src/lib/speech-support.ts');

test('finite emotion choices contain only native values and retain scope restrictions', () => {
  const route = {
    status: 'documented',
    instructions: 'none',
    emotion: {
      mode: 'enum',
      tags: [
        'angry',
        'disgusted',
        'sad',
        'happy',
        'fearful',
        'neutral',
        'surprised'
      ]
    }
  };
  assert.deepEqual(emotionChoices(route), route.emotion.tags);
  assert.match(emotionChoiceKind(route), /Native/);
  for (const value of emotionChoices(route)) {
    assert.equal(deliverySupport(route, 'emotion', value).status, 'sent');
    assert.equal(
      deliverySupport(route, 'emotion', value, 'phrase').status,
      'unsupported'
    );
  }
  assert.equal(
    deliverySupport(route, 'emotion', 'wistful, holding back tears').status,
    'unsupported'
  );
});

test('descriptive models offer suggestions without treating them as an allowlist', () => {
  const route = {
    status: 'documented',
    dialect: 'gemini38',
    instructions: 'field',
    emotion: { mode: 'open_description', tags: [] }
  };
  assert.ok(emotionChoices(route).includes('wistful'));
  assert.match(emotionChoiceKind(route), /Suggested/);
  assert.equal(
    deliverySupport(
      route,
      'emotion',
      'quietly hopeful, despite exhaustion',
      'phrase'
    ).status,
    'hint'
  );
  assert.equal(
    deliverySupport(route, 'emotion', 'wistful', 'phrase', false).status,
    'disabled'
  );
});

test('unsupported and unknown routes still offer authoring choices with distinct feedback', () => {
  const cloning = {
    status: 'documented',
    instructions: 'none',
    emotion: { mode: 'none', tags: [] }
  };
  assert.ok(emotionChoices(cloning).includes('happy'));
  assert.match(emotionChoiceKind(cloning), /Authoring suggestions/);
  assert.equal(
    deliverySupport(cloning, 'emotion', 'happy').status,
    'unsupported'
  );
  assert.match(emotionChoiceKind(), /unverified/);
  assert.equal(
    deliverySupport(undefined, 'emotion', 'happy').status,
    'unknown'
  );
});
