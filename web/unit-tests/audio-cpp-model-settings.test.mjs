import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { Buffer } from 'node:buffer';
import { transformSync } from 'esbuild';

const source = readFileSync(
  new URL('../src/lib/audio-cpp-model-settings.ts', import.meta.url),
  'utf8'
);
const compiled = transformSync(source, { loader: 'ts', format: 'esm' }).code;
const { mergeModelTuningMaps, updateModelTuning } = await import(
  `data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`
);

test('model defaults replace inherited tuning even after a global addition', () => {
  const globals = {
    selected: { temperature: 0.8, seed: 7 },
    other: { speed: 1.2 }
  };
  const overrides = updateModelTuning({}, 'selected', 'defaults');
  assert.deepEqual(mergeModelTuningMaps(globals, overrides), {
    selected: null,
    other: { speed: 1.2 }
  });
  globals.selected.top_k = 4;
  assert.equal(mergeModelTuningMaps(globals, overrides).selected, null);
  assert.deepEqual(globals.selected, { temperature: 0.8, seed: 7, top_k: 4 });
});

test('inheriting removes only the selected project override', () => {
  const overrides = { selected: null, other: { seed: 9 } };
  const inherited = updateModelTuning(overrides, 'selected', 'inherit');
  assert.deepEqual(inherited, { other: { seed: 9 } });
  assert.deepEqual(mergeModelTuningMaps({ selected: { seed: 3 } }, inherited), {
    selected: { seed: 3 },
    other: { seed: 9 }
  });
  assert.deepEqual(overrides, { selected: null, other: { seed: 9 } });
});

test('custom controls inherit unspecified values and retain explicit per-key defaults', () => {
  const changed = updateModelTuning({}, 'selected', {
    key: 'temperature',
    value: 0.4
  });
  const reset = updateModelTuning(changed, 'selected', {
    key: 'seed',
    value: ''
  });
  assert.deepEqual(
    mergeModelTuningMaps(
      { selected: { temperature: 0.9, seed: 8, top_k: 2 } },
      reset
    ),
    { selected: { temperature: 0.4, seed: null, top_k: 2 } }
  );
});

test('editing after a whole-model reset starts from model defaults', () => {
  const changed = updateModelTuning(
    { selected: null },
    'selected',
    { key: 'temperature', value: 0.4 },
    { temperature: null, seed: null }
  );
  assert.deepEqual(
    mergeModelTuningMaps({ selected: { temperature: 0.9, seed: 8 } }, changed),
    { selected: { temperature: 0.4, seed: null } }
  );
});
