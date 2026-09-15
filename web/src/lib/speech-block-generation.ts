/**
 * Passage-first speech-block generation settings (frontend domain module).
 *
 * Backend contract (TTS section, backend-owned):
 * - speech_block_generation_mode: 'passage' (default) | 'legacy'
 * - speech_block_regroup_enabled: boolean, default false
 * - speech_block_regroup_max_mismatch_ms: integer 0..2000, default 500
 * - speech_block_regroup_max_mismatch_percent: integer 0..50, default 15
 * - speech_block_regroup_max_gap_ms: integer 0..2000, default 300
 * - speech_block_regroup_max_passages: integer 2..8, default 3
 * - speech_block_regroup_max_boundary_shift_ms: integer 0..2000, default 500
 *
 * Semantics (human-precise, per approved plan):
 * - Default is "just generate passages": one TTS request per passage.
 * - Optional second pass concatenates eligible passage *texts* and regenerates
 *   short same-voice groups with the same TTS model. It is not waveform
 *   concatenation and uses no alignment model.
 * - Duration-based only, not measured internal alignment. Both the ms and the
 *   percent mismatch thresholds must pass.
 * - Candidates crossing overlap/cut/speaker/user boundaries are excluded;
 *   existing speech_block_max_chars still caps combined spoken/display text.
 * - Original first-pass takes are retained as fallback; no recursive grouping.
 * - Early-repair (recursive split) controls belong to legacy grouping only and
 *   must not be implied by passage mode.
 *
 * Editing these values never writes anything by itself. Callers must read the
 * saved effective settings, coerce them for display, and save explicitly.
 * Unknown keys are never sent.
 */

export const SPEECH_BLOCK_GENERATION_SECTION = 'tts';

export type SpeechBlockGenerationMode = 'passage' | 'legacy';

export const SPEECH_BLOCK_GENERATION_MODES: SpeechBlockGenerationMode[] = [
  'passage',
  'legacy'
];

export type SpeechBlockGenerationValues = {
  speech_block_generation_mode: SpeechBlockGenerationMode;
  speech_block_regroup_enabled: boolean;
  speech_block_regroup_max_mismatch_ms: number;
  speech_block_regroup_max_mismatch_percent: number;
  speech_block_regroup_max_gap_ms: number;
  speech_block_regroup_max_passages: number;
  speech_block_regroup_max_boundary_shift_ms: number;
};

export const SPEECH_BLOCK_GENERATION_DEFAULTS: SpeechBlockGenerationValues = {
  speech_block_generation_mode: 'passage',
  speech_block_regroup_enabled: false,
  speech_block_regroup_max_mismatch_ms: 500,
  speech_block_regroup_max_mismatch_percent: 15,
  speech_block_regroup_max_gap_ms: 300,
  speech_block_regroup_max_passages: 3,
  speech_block_regroup_max_boundary_shift_ms: 500
};

export type SpeechBlockRegroupKey =
  | 'speech_block_regroup_max_mismatch_ms'
  | 'speech_block_regroup_max_mismatch_percent'
  | 'speech_block_regroup_max_gap_ms'
  | 'speech_block_regroup_max_passages'
  | 'speech_block_regroup_max_boundary_shift_ms';

export type SpeechBlockRegroupControl = {
  key: SpeechBlockRegroupKey;
  label: string;
  min: number;
  max: number;
  step: number;
  unit: string;
  help: string;
};

export const SPEECH_BLOCK_REGROUP_CONTROLS: SpeechBlockRegroupControl[] = [
  {
    key: 'speech_block_regroup_max_mismatch_ms',
    label: 'Maximum duration mismatch (ms)',
    min: 0,
    max: 2000,
    step: 10,
    unit: 'milliseconds',
    help: 'Estimated combined spoken audio must fit the source span within this many ms. Both the ms and the percent limits must pass.'
  },
  {
    key: 'speech_block_regroup_max_mismatch_percent',
    label: 'Maximum duration mismatch (%)',
    min: 0,
    max: 50,
    step: 1,
    unit: 'percent',
    help: 'Estimated combined spoken audio must fit the source span within this percent. Both the ms and the percent limits must pass.'
  },
  {
    key: 'speech_block_regroup_max_gap_ms',
    label: 'Maximum source pause (ms)',
    min: 0,
    max: 2000,
    step: 10,
    unit: 'milliseconds',
    help: 'Passages separated by a longer source pause are never combined.'
  },
  {
    key: 'speech_block_regroup_max_passages',
    label: 'Maximum passages per group',
    min: 2,
    max: 8,
    step: 1,
    unit: 'passages',
    help: 'Short same-voice groups only. No recursive grouping.'
  },
  {
    key: 'speech_block_regroup_max_boundary_shift_ms',
    label: 'Maximum boundary shift (ms)',
    min: 0,
    max: 2000,
    step: 10,
    unit: 'milliseconds',
    help: 'Estimated cumulative boundary movement for the group must stay within this budget.'
  }
];

const CONTROL_BY_KEY = new Map(
  SPEECH_BLOCK_REGROUP_CONTROLS.map((control) => [control.key, control])
);

export function speechBlockRegroupControl(key: SpeechBlockRegroupKey) {
  return CONTROL_BY_KEY.get(key);
}

export function isSpeechBlockGenerationMode(
  value: unknown
): value is SpeechBlockGenerationMode {
  return value === 'passage' || value === 'legacy';
}

export function isPassageGenerationMode(value: unknown): boolean {
  return value === 'passage';
}

/** Fill missing/invalid entries from built-in defaults without mutating input. */
export function coerceSpeechBlockGenerationValues(
  saved: Record<string, unknown> | null | undefined
): SpeechBlockGenerationValues {
  const result = { ...SPEECH_BLOCK_GENERATION_DEFAULTS };
  if (!saved) return result;
  const rawMode = saved.speech_block_generation_mode;
  if (isSpeechBlockGenerationMode(rawMode)) {
    result.speech_block_generation_mode = rawMode;
  }
  const rawEnabled = saved.speech_block_regroup_enabled;
  if (typeof rawEnabled === 'boolean') {
    result.speech_block_regroup_enabled = rawEnabled;
  }
  for (const control of SPEECH_BLOCK_REGROUP_CONTROLS) {
    const raw = saved[control.key];
    if (typeof raw === 'boolean') continue;
    const value = Number(raw);
    if (Number.isInteger(value)) result[control.key] = value;
  }
  return result;
}

/** validation_error-compatible messages keyed by field; empty means valid. */
export function validateSpeechBlockGenerationValues(
  values: Record<string, unknown>
): Partial<Record<keyof SpeechBlockGenerationValues, string>> {
  const errors: Partial<Record<keyof SpeechBlockGenerationValues, string>> = {};
  const mode = values.speech_block_generation_mode;
  if (
    mode !== undefined &&
    mode !== null &&
    mode !== '' &&
    !isSpeechBlockGenerationMode(mode)
  ) {
    errors.speech_block_generation_mode = "Choose 'passage' or 'legacy'.";
  }
  const enabled = values.speech_block_regroup_enabled;
  if (
    enabled !== undefined &&
    enabled !== null &&
    typeof enabled !== 'boolean'
  ) {
    errors.speech_block_regroup_enabled = 'Choose on or off.';
  }
  for (const control of SPEECH_BLOCK_REGROUP_CONTROLS) {
    const raw = values[control.key];
    if (typeof raw === 'boolean' || raw === '' || raw == null) {
      errors[control.key] = 'Enter a whole number.';
      continue;
    }
    const value = Number(raw);
    if (!Number.isInteger(value)) {
      errors[control.key] = 'Enter a whole number.';
      continue;
    }
    if (value < control.min || value > control.max) {
      errors[control.key] =
        `Enter ${control.min} to ${control.max} ${control.unit}.`;
    }
  }
  return errors;
}

export function speechBlockGenerationValuesEqual(
  left: SpeechBlockGenerationValues,
  right: SpeechBlockGenerationValues
): boolean {
  return (
    left.speech_block_generation_mode === right.speech_block_generation_mode &&
    left.speech_block_regroup_enabled === right.speech_block_regroup_enabled &&
    SPEECH_BLOCK_REGROUP_CONTROLS.every(({ key }) => left[key] === right[key])
  );
}

/** Exact seven-key payload; unknown keys are never sent (backend rejects them). */
export function speechBlockGenerationPayload(
  values: Record<string, unknown>
): SpeechBlockGenerationValues {
  const coerced = coerceSpeechBlockGenerationValues(values);
  return {
    speech_block_generation_mode: coerced.speech_block_generation_mode,
    speech_block_regroup_enabled: coerced.speech_block_regroup_enabled,
    speech_block_regroup_max_mismatch_ms:
      coerced.speech_block_regroup_max_mismatch_ms,
    speech_block_regroup_max_mismatch_percent:
      coerced.speech_block_regroup_max_mismatch_percent,
    speech_block_regroup_max_gap_ms: coerced.speech_block_regroup_max_gap_ms,
    speech_block_regroup_max_passages:
      coerced.speech_block_regroup_max_passages,
    speech_block_regroup_max_boundary_shift_ms:
      coerced.speech_block_regroup_max_boundary_shift_ms
  };
}
