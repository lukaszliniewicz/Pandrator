import type { RuntimeCapabilities } from './api-models';

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
  return `${selected === 'parakeet' ? 'Parakeet' : selected === 'moss' ? 'MOSS' : selected === 'whisper' ? 'Whisper' : selected} does not support ${code}. Choose another model or correct the source language.`;
}
