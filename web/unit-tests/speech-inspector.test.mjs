import assert from 'node:assert/strict';
import { Buffer } from 'node:buffer';
import { test } from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/lib/speech-inspector.ts'],
  bundle: true,
  write: false,
  format: 'esm',
  platform: 'browser'
});
const {
  deliveryPatch,
  inspectorTargetKey,
  currentSpeakerValue,
  localDelivery,
  inheritDeliveryPatch
} = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('editing one mixed direction preserves all untouched fields', () => {
  const current = {
    instruction: null,
    emotion: null,
    pace: 'brisk',
    cadence: ''
  };
  assert.deepEqual(deliveryPatch(current, { instruction: '  Softly  ' }), {
    instruction: 'Softly'
  });
  assert.deepEqual(deliveryPatch(current, {}), {});
  assert.deepEqual(deliveryPatch(current, { emotion: '' }), { emotion: null });
});

test('clearing is explicit, unchanged or whitespace-only edits are omitted', () => {
  assert.deepEqual(
    deliveryPatch(
      { instruction: 'Softly', pace: '' },
      { instruction: '', pace: ' ' }
    ),
    { instruction: null }
  );
  assert.deepEqual(
    deliveryPatch({ instruction: 'Softly' }, { instruction: ' Softly ' }),
    {}
  );
});

test('a routine payload refresh keeps the inspector identity; history does not', () => {
  const target = {
    revisionId: 'plan',
    item: { id: 'segment', revision: 1 },
    start: 2,
    end: 5
  };
  assert.equal(
    inspectorTargetKey(target),
    inspectorTargetKey({
      ...target,
      item: { ...target.item, audio_takes: [] },
      focus: 'delivery'
    })
  );
  assert.notEqual(
    inspectorTargetKey(target),
    inspectorTargetKey({ ...target, runId: 'old-run' })
  );
});

test('mixed speakers are left intact until a user chooses a role', () => {
  assert.equal(
    currentSpeakerValue({ current: { speaker: 'mixed' } }),
    '__keep__'
  );
  assert.equal(
    currentSpeakerValue({ current: { speaker: 'dialogue' } }),
    '__dialogue__'
  );
  assert.equal(
    currentSpeakerValue({
      current: { speaker: 'character', character_id: 'fred' }
    }),
    'fred'
  );
});

test('phrase authoring preserves root instructions and resets to inherited delivery', () => {
  const root = { instruction: 'Read warmly', pace: 'slower', emotion: 'happy' };
  const effective = {
    instruction: 'Read warmly\nEmphasize this',
    pace: 'slower',
    emotion: null
  };
  const local = localDelivery(effective, root);
  assert.deepEqual(local, {
    instruction: 'Emphasize this',
    pace: '',
    emotion: null
  });
  const changed = deliveryPatch(local, {
    instruction: 'Lower voice',
    pace: 'brisk'
  });
  assert.deepEqual(inheritDeliveryPatch(root, changed), {
    instruction: 'Read warmly\nLower voice',
    pace: 'brisk'
  });
  assert.deepEqual(
    inheritDeliveryPatch(root, { instruction: null, emotion: null }),
    {
      instruction: 'Read warmly',
      emotion: 'happy'
    }
  );
  assert.deepEqual(localDelivery({ instruction: 'Read warmly' }, root), {
    instruction: ''
  });
  assert.deepEqual(localDelivery({ instruction: 'Other\nRead warmly' }, root), {
    instruction: 'Other\nRead warmly'
  });
});

test('local emotion intensity preserves zero and restores the inherited scalar on clear', () => {
  const root = { emotion: 'happy', emotion_intensity: 0.75 };
  assert.deepEqual(
    localDelivery({ emotion: 'happy', emotion_intensity: 0.75 }, root),
    { emotion: '' }
  );
  assert.deepEqual(
    localDelivery({ emotion: 'sad', emotion_intensity: 0 }, root),
    { emotion: 'sad', emotion_intensity: 0 }
  );
  assert.deepEqual(
    inheritDeliveryPatch(root, { emotion: null, emotion_intensity: null }),
    { emotion: 'happy', emotion_intensity: 0.75 }
  );
});
