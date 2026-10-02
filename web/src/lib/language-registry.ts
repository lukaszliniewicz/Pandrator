import registry from '../../../pandrator/logic/language_registry.json' with { type: 'json' };

export type LanguageOption = { value: string; label: string };
export type LanguageSupport = {
  schema_version: number;
  catalogue_revision: string;
  provider_id: string;
  model_id: string;
  model_revision: string;
  operation: string;
  native_route: string;
  coverage: 'exact' | 'subset' | 'claim' | 'unknown' | 'independent';
  languages: string[];
  request_aliases?: Record<string, string>;
  source_key?: string;
  source_revision?: string | null;
  runtime_requirement?: string;
  discovery?: string;
  note?: string;
};

const normalize = (value: string) =>
  value.trim().toLowerCase().replaceAll('_', '-');
const aliases = new Map<string, string | null>();
const labels = new Map<string, string>();
const searchTerms = new Map<string, string>();
for (const entry of registry.languages) {
  labels.set(entry.tag, entry.label);
  searchTerms.set(
    entry.tag,
    [entry.tag, entry.label, ...entry.aliases].join(' ').toLowerCase()
  );
  for (const raw of [entry.tag, entry.label, ...entry.aliases]) {
    const alias = normalize(raw);
    if (!aliases.has(alias)) aliases.set(alias, entry.tag);
    else if (aliases.get(alias) !== entry.tag) aliases.set(alias, null);
  }
}

export function canonicalLanguageTag(value: string, allowAuto = false): string {
  const tag = normalize(value);
  if (!tag) return '';
  if (['auto', 'automatic', 'detect', 'und', 'unknown'].includes(tag))
    return allowAuto ? 'auto' : '';
  return (
    aliases.get(tag) || (/^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$/.test(tag) ? tag : '')
  );
}

export function languageLabel(value: string): string {
  const code = canonicalLanguageTag(value, true);
  return code === 'auto' ? 'Automatic detection' : labels.get(code) || value;
}

export function languageMatches(actual: string, requested: string): boolean {
  const a = canonicalLanguageTag(actual);
  const b = canonicalLanguageTag(requested);
  return Boolean(
    a &&
    b &&
    (a === b ||
      (!a.includes('-') && b.startsWith(`${a}-`)) ||
      (!b.includes('-') && a.startsWith(`${b}-`)))
  );
}

export function languageSearchMatches(code: string, search: string): boolean {
  const tag = canonicalLanguageTag(code, true);
  return (
    searchTerms.get(tag) || `${languageLabel(code)} ${code}`.toLowerCase()
  ).includes(search.trim().toLowerCase());
}

// Familiar choices lead the complete registry; registry membership is a name,
// not a claim that a selected provider or operation supports the language.
const common = [
  'en',
  'pl',
  'de',
  'fr',
  'es',
  'it',
  'pt',
  'pt-br',
  'nl',
  'sv',
  'no',
  'nb',
  'nn',
  'da',
  'fi',
  'cs',
  'sk',
  'uk',
  'ru',
  'bg',
  'ro',
  'hu',
  'el',
  'tr',
  'ar',
  'he',
  'fa',
  'hi',
  'bn',
  'ur',
  'zh',
  'yue',
  'fil',
  'tl',
  'ja',
  'ko',
  'vi',
  'th',
  'id',
  'ms'
];
const order = new Map(common.map((code, index) => [code, index]));
export const REGISTRY_LANGUAGE_OPTIONS: LanguageOption[] = registry.languages
  .map((entry) => ({ value: entry.tag, label: entry.label }))
  .sort(
    (a, b) =>
      (order.get(a.value) ?? 1000) - (order.get(b.value) ?? 1000) ||
      a.label.localeCompare(b.label)
  );
export const LANGUAGE_OPTIONS: LanguageOption[] = [
  { value: 'auto', label: 'Automatic detection' },
  ...REGISTRY_LANGUAGE_OPTIONS
];

export function languageSupportProblem(
  record: LanguageSupport | undefined,
  language: string
): string {
  if (
    !record ||
    record.coverage !== 'exact' ||
    !language ||
    language === 'auto'
  )
    return '';
  return record.languages.some((code) => languageMatches(code, language))
    ? ''
    : `${record.model_id} does not support ${languageLabel(language)} for ${record.operation}. Choose a supported language or another model.`;
}
