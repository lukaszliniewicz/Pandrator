import { expect, test } from '@playwright/test';
import {
  SPEECH_BLOCK_GENERATION_DEFAULTS,
  SPEECH_BLOCK_REGROUP_CONTROLS,
  coerceSpeechBlockGenerationValues,
  isSpeechBlockGenerationMode,
  speechBlockGenerationPayload,
  speechBlockGenerationValuesEqual,
  validateSpeechBlockGenerationValues
} from '../src/lib/speech-block-generation';
import {
  numberPresentation,
  optionsFor,
  settingLabel
} from '../src/lib/settings-fields';

test('speech-block generation defaults match the approved contract', () => {
  expect(SPEECH_BLOCK_GENERATION_DEFAULTS).toEqual({
    speech_block_generation_mode: 'passage',
    speech_block_regroup_enabled: false,
    speech_block_regroup_max_mismatch_ms: 500,
    speech_block_regroup_max_mismatch_percent: 15,
    speech_block_regroup_max_gap_ms: 300,
    speech_block_regroup_max_passages: 3,
    speech_block_regroup_max_boundary_shift_ms: 500
  });
});

test('speech-block generation mode accepts only passage or legacy', () => {
  expect(isSpeechBlockGenerationMode('passage')).toBe(true);
  expect(isSpeechBlockGenerationMode('legacy')).toBe(true);
  expect(isSpeechBlockGenerationMode('auto')).toBe(false);
  expect(isSpeechBlockGenerationMode('')).toBe(false);
  expect(isSpeechBlockGenerationMode(undefined)).toBe(false);
});

test('speech-block generation coercion preserves saved values without silent rewrites', () => {
  expect(coerceSpeechBlockGenerationValues(null)).toEqual(
    SPEECH_BLOCK_GENERATION_DEFAULTS
  );
  expect(coerceSpeechBlockGenerationValues(undefined)).toEqual(
    SPEECH_BLOCK_GENERATION_DEFAULTS
  );
  // Unknown mode falls back to passage; explicit legacy is preserved.
  expect(
    coerceSpeechBlockGenerationValues({ speech_block_generation_mode: 'auto' })
      .speech_block_generation_mode
  ).toBe('passage');
  expect(
    coerceSpeechBlockGenerationValues({
      speech_block_generation_mode: 'legacy'
    }).speech_block_generation_mode
  ).toBe('legacy');
  // Booleans never become numeric thresholds; numeric strings coerce.
  expect(
    coerceSpeechBlockGenerationValues({
      speech_block_regroup_enabled: true,
      speech_block_regroup_max_mismatch_ms: true,
      speech_block_regroup_max_mismatch_percent: '20'
    })
  ).toEqual({
    ...SPEECH_BLOCK_GENERATION_DEFAULTS,
    speech_block_regroup_enabled: true,
    speech_block_regroup_max_mismatch_percent: 20
  });
  expect(
    coerceSpeechBlockGenerationValues({
      speech_block_regroup_max_passages: 2.5
    }).speech_block_regroup_max_passages
  ).toBe(SPEECH_BLOCK_GENERATION_DEFAULTS.speech_block_regroup_max_passages);
});

test('speech-block generation validation enforces UI/backend bounds', () => {
  expect(
    validateSpeechBlockGenerationValues({
      ...SPEECH_BLOCK_GENERATION_DEFAULTS
    })
  ).toEqual({});
  const errors = validateSpeechBlockGenerationValues({
    ...SPEECH_BLOCK_GENERATION_DEFAULTS,
    speech_block_generation_mode: 'auto',
    speech_block_regroup_enabled: 'yes',
    speech_block_regroup_max_mismatch_ms: 2001,
    speech_block_regroup_max_mismatch_percent: 51,
    speech_block_regroup_max_gap_ms: -1,
    speech_block_regroup_max_passages: 9,
    speech_block_regroup_max_boundary_shift_ms: 2001
  });
  expect(Object.keys(errors).sort()).toEqual([
    'speech_block_generation_mode',
    'speech_block_regroup_enabled',
    'speech_block_regroup_max_boundary_shift_ms',
    'speech_block_regroup_max_gap_ms',
    'speech_block_regroup_max_mismatch_ms',
    'speech_block_regroup_max_mismatch_percent',
    'speech_block_regroup_max_passages'
  ]);
  // Lower bound for passages is 2: single-passage groups are meaningless.
  expect(
    validateSpeechBlockGenerationValues({
      ...SPEECH_BLOCK_GENERATION_DEFAULTS,
      speech_block_regroup_max_passages: 1
    }).speech_block_regroup_max_passages
  ).toBeTruthy();
});

test('speech-block generation payload never carries unknown keys', () => {
  expect(
    speechBlockGenerationPayload({
      ...SPEECH_BLOCK_GENERATION_DEFAULTS,
      injected: 'x'
    })
  ).toEqual(SPEECH_BLOCK_GENERATION_DEFAULTS);
});

test('speech-block generation equality distinguishes mode and thresholds', () => {
  expect(
    speechBlockGenerationValuesEqual(SPEECH_BLOCK_GENERATION_DEFAULTS, {
      ...SPEECH_BLOCK_GENERATION_DEFAULTS
    })
  ).toBe(true);
  expect(
    speechBlockGenerationValuesEqual(SPEECH_BLOCK_GENERATION_DEFAULTS, {
      ...SPEECH_BLOCK_GENERATION_DEFAULTS,
      speech_block_generation_mode: 'legacy'
    })
  ).toBe(false);
  expect(
    speechBlockGenerationValuesEqual(SPEECH_BLOCK_GENERATION_DEFAULTS, {
      ...SPEECH_BLOCK_GENERATION_DEFAULTS,
      speech_block_regroup_max_passages: 4
    })
  ).toBe(false);
});

test('speech-block generation settings metadata stays meaningful and bounded', () => {
  const modeOptions = optionsFor('tts', 'speech_block_generation_mode');
  expect(modeOptions).toEqual([
    { value: 'passage', label: 'Separate passages (default)' },
    { value: 'legacy', label: 'Legacy grouping' }
  ]);
  // Maximum, not target: combined spoken/display text.
  expect(settingLabel('speech_block_max_chars')).toContain('Maximum');
  expect(settingLabel('speech_block_max_chars')).not.toContain('Target');
  expect(settingLabel('speech_block_generation_mode')).toBe('Generation mode');
  const bounds: Record<string, { min: number; max: number }> = {
    speech_block_regroup_max_mismatch_ms: { min: 0, max: 2000 },
    speech_block_regroup_max_mismatch_percent: { min: 0, max: 50 },
    speech_block_regroup_max_gap_ms: { min: 0, max: 2000 },
    speech_block_regroup_max_passages: { min: 2, max: 8 },
    speech_block_regroup_max_boundary_shift_ms: { min: 0, max: 2000 }
  };
  for (const [key, expected] of Object.entries(bounds)) {
    const meta = numberPresentation(key);
    expect(meta.min).toBe(expected.min);
    expect(meta.max).toBe(expected.max);
  }
  expect(SPEECH_BLOCK_REGROUP_CONTROLS.map((item) => item.key).sort()).toEqual(
    Object.keys(bounds).sort()
  );
});
