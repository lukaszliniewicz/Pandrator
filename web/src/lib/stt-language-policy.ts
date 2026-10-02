import type { RuntimeCapabilities } from './api-models';
import {
  canonicalLanguageTag,
  languageLabel,
  LANGUAGE_OPTIONS
} from './language-registry';

// Qwen's recognizer and forced aligner have different language coverage.
// Source: QwenLM/Qwen3-ASR model table, verified 2026-09-22.
const QWEN_ALIGNMENT_LANGUAGES = new Set([
  'zh',
  'en',
  'yue',
  'fr',
  'de',
  'it',
  'ja',
  'ko',
  'pt',
  'ru',
  'es'
]);
const qwenLanguageCode = (language: unknown) => {
  const value = String(language ?? '')
    .trim()
    .toLowerCase()
    .replaceAll('_', '-')
    .split('-')[0];
  return value === 'tl' ? 'fil' : value;
};

/** Explain timestamp behavior without calling an unsupported language "unsupported ASR". */
export function qwenTimingExplanation(
  capabilities: RuntimeCapabilities | null | undefined,
  language: unknown
): string {
  const code = qwenLanguageCode(language);
  if (!code || code === 'auto')
    return 'Automatic detection uses a short local sample, then chooses an aligner for the detected source language. Choose a source language explicitly if detection cannot resolve it.';
  if (QWEN_ALIGNMENT_LANGUAGES.has(code))
    return 'Word timestamps: Qwen3 Forced Aligner will align the recognized text to the recording.';
  const timed = capabilities?.stt?.models?.qwen3?.timed_supported_languages;
  if (Array.isArray(timed) && timed.includes(code))
    return 'Word timestamps: this language uses Canary CTC alignment after Qwen recognition. Qwen’s own aligner does not cover this language.';
  return 'Qwen may recognize this language, but its own aligner does not support it. Timed transcription requires a supported alignment path; choose Whisper when no fallback is available.';
}

/** Timed-pipeline gate; the recognizer-only language list remains broader. */
export function qwenTimedLanguageProblem(
  capabilities: RuntimeCapabilities | null | undefined,
  language: unknown
): string {
  const code = qwenLanguageCode(language);
  const info = capabilities?.stt?.models?.qwen3;
  if (
    (!code || code === 'auto') &&
    info?.requires_explicit_language_for_timestamps === true
  )
    return 'Choose the source language for Qwen timed transcription before starting.';
  if (
    code &&
    code !== 'auto' &&
    Array.isArray(info?.timed_supported_languages) &&
    !info.timed_supported_languages.includes(code)
  )
    return `Qwen recognition and word alignment have different language coverage. Timed transcription for ${code} is not supported by the configured aligners; choose Whisper or another timed recognizer.`;
  return '';
}

/** Unknown coverage stays selectable. Only authoritative, explicit lists restrict it. */
export function sttLanguageProblem(
  capabilities: RuntimeCapabilities | null | undefined,
  engine: string,
  language: unknown
): string {
  const code = String(language ?? '')
    .trim()
    .toLowerCase()
    .replaceAll('_', '-');
  if (!code || code === 'auto') return '';
  const selected = engine || capabilities?.stt?.default_engine || '';
  const supported = capabilities?.stt?.models?.[selected]?.supported_languages;
  if (!Array.isArray(supported) || !supported.length) return '';
  const normalized = code.split('-')[0];
  const aliases: Record<string, string> = {
    nb: 'no',
    iw: 'he',
    jv: 'jw',
    ptbr: 'pt',
    zhcn: 'zh'
  };
  const base = aliases[normalized] || normalized;
  if (supported.includes(base) || supported.includes(code)) return '';
  return `${selected === 'parakeet' ? 'Parakeet' : selected === 'moss' ? 'MOSS' : selected === 'whisper' ? 'Whisper' : selected === 'qwen3' ? 'Qwen3 ASR' : selected} does not support ${code}. Choose another model or correct the source language.`;
}

export function sttLanguageOptions(
  capabilities: RuntimeCapabilities | null | undefined,
  engine: string,
  requireTiming = true
) {
  const models = capabilities?.stt?.models;
  const selected = engine || capabilities?.stt?.default_engine || 'auto';
  const codes =
    selected === 'auto'
      ? [
          ...new Set(
            Object.entries(models ?? {}).flatMap(([key, model]) =>
              key === 'auto'
                ? []
                : ((key === 'qwen3' && requireTiming
                    ? model.timed_supported_languages
                    : model.supported_languages) ?? [])
            )
          )
        ]
      : selected === 'qwen3' && requireTiming
        ? models?.[selected]?.timed_supported_languages
        : models?.[selected]?.supported_languages;
  if (!Array.isArray(codes) || !codes.length) return LANGUAGE_OPTIONS;
  return [
    { value: 'auto', label: 'Automatic detection' },
    ...[
      ...new Set(codes.map((code) => canonicalLanguageTag(code) || code))
    ].map((value) => ({ value, label: languageLabel(value) }))
  ];
}
