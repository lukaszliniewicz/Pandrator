import {
  canonicalLanguageTag,
  languageLabel,
  languageMatches,
  type LanguageSupport
} from './language-registry';
import type { TtsService } from './api-models';

export type VoiceDescriptor = {
  id: string;
  name: string;
  languageCode: string;
  language: string;
  gender: string;
};

export type ModelLanguageMetadata = {
  family?: string;
  language_support?: LanguageSupport;
  supported_languages?: readonly string[];
  id?: string;
  languages?: readonly (
    string | { language_id?: string; id?: string; code?: string; name?: string }
  )[];
};

const LANGUAGE_LABELS: Record<string, string> = {
  ar: 'Arabic',
  de: 'German',
  en: 'American English',
  'en-us': 'American English',
  'en-gb': 'British English',
  es: 'Spanish',
  fr: 'French',
  hi: 'Hindi',
  it: 'Italian',
  ja: 'Japanese',
  ko: 'Korean',
  nl: 'Dutch',
  pl: 'Polish',
  pt: 'Portuguese',
  ru: 'Russian',
  tr: 'Turkish',
  cs: 'Czech',
  hu: 'Hungarian',
  vi: 'Vietnamese',
  'zh-cn': 'Chinese (Simplified)',
  zh: 'Chinese',
  as: 'Assamese',
  aze: 'Azerbaijani',
  bak: 'Bashkir',
  bel: 'Belarusian',
  bn: 'Bengali',
  chv: 'Chuvash',
  erz: 'Erzya',
  gu: 'Gujarati',
  hye: 'Armenian',
  kat: 'Georgian',
  kaz: 'Kazakh',
  kbd: 'Kabardian-Cherkess',
  kir: 'Kyrgyz',
  kjh: 'Khakas',
  kn: 'Kannada',
  mdf: 'Moksha',
  ml: 'Malayalam',
  mni: 'Manipuri',
  raj: 'Rajasthani',
  sah: 'Yakut',
  ta: 'Tamil',
  tat: 'Tatar',
  te: 'Telugu',
  tgk: 'Tajik',
  udm: 'Udmurt',
  ukr: 'Ukrainian',
  uzb: 'Uzbek',
  xal: 'Kalmyk',
  indic: 'Indic languages',
  'en-in': 'Indian English'
};

const KOKORO_PREFIX_LANGUAGES: Record<string, string> = {
  a: 'en-us',
  b: 'en-gb',
  d: 'de',
  e: 'es',
  f: 'fr',
  h: 'hi',
  i: 'it',
  j: 'ja',
  p: 'pt',
  z: 'zh-cn'
};

const QWEN_PRESET_VOICES: Record<
  string,
  { name: string; language: string; gender: string }
> = {
  aiden: {
    name: 'Aiden',
    language: 'Native accent: American English',
    gender: 'Male'
  },
  dylan: {
    name: 'Dylan',
    language: 'Native accent: Beijing Chinese',
    gender: 'Male'
  },
  eric: {
    name: 'Eric',
    language: 'Native accent: Sichuan Chinese',
    gender: 'Male'
  },
  ono_anna: {
    name: 'Ono Anna',
    language: 'Native language: Japanese',
    gender: 'Female'
  },
  ryan: { name: 'Ryan', language: 'Native language: English', gender: 'Male' },
  serena: {
    name: 'Serena',
    language: 'Native language: Chinese',
    gender: 'Female'
  },
  sohee: {
    name: 'Sohee',
    language: 'Native language: Korean',
    gender: 'Female'
  },
  uncle_fu: {
    name: 'Uncle Fu',
    language: 'Native language: Chinese',
    gender: 'Male'
  },
  vivian: {
    name: 'Vivian',
    language: 'Native language: Chinese',
    gender: 'Female'
  }
};

const VOXTRAL_LANGUAGES = [
  'ar',
  'en',
  'de',
  'es',
  'fr',
  'hi',
  'it',
  'nl',
  'pt'
];
const MAGPIE_LOCALES: Record<string, string> = {
  'EN-US': 'en',
  'ES-US': 'es',
  'FR-FR': 'fr',
  'DE-DE': 'de',
  'VI-VN': 'vi',
  'IT-IT': 'it',
  'ZH-CN': 'zh',
  'HI-IN': 'hi',
  'JA-JP': 'ja'
};

const titleize = (value: string) =>
  value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((token) => token.charAt(0).toUpperCase() + token.slice(1))
    .join(' ');

function kokoroDescriptor(id: string): VoiceDescriptor {
  const first = id
    .split('+')[0]
    .trim()
    .replace(/\(\s*\d+(?:\.\d+)?\s*\)$/, '');
  const match = first.match(/^([abdefhijpz])([fm])_(.+)$/i);
  if (match) {
    const languageCode = KOKORO_PREFIX_LANGUAGES[match[1].toLowerCase()] ?? '';
    const gender = match[2].toLowerCase() === 'f' ? 'Female' : 'Male';
    const names = id.split('+').map((part) => {
      const clean = part.trim().replace(/\(\s*\d+(?:\.\d+)?\s*\)$/, '');
      const component = clean.match(/^[abdefhijpz][fm]_(.+)$/i);
      return titleize(component?.[1] ?? clean);
    });
    return {
      id,
      name: names.length > 1 ? `Blend: ${names.join(' + ')}` : names[0],
      languageCode,
      language: LANGUAGE_LABELS[languageCode] ?? languageCode,
      gender
    };
  }
  if (first.toLowerCase() === 'martin')
    return {
      id,
      name: 'Martin',
      languageCode: 'de',
      language: 'German',
      gender: 'Male'
    };
  return {
    id,
    name: titleize(first),
    languageCode: '',
    language: 'Multilingual',
    gender: ''
  };
}

export function describeVoice(
  serviceId: string,
  voiceId: string,
  metadata?: Record<string, unknown>
): VoiceDescriptor {
  const service = serviceId.toLowerCase().replaceAll('-', '_');
  if (metadata?.pandrator_preset === true) {
    const languageCode = canonicalLanguageTag(
      String(metadata.language_code ?? '')
    );
    return {
      id: voiceId,
      name: String(metadata.display_name ?? metadata.name ?? titleize(voiceId)),
      languageCode: metadata.multilingual === true ? '' : languageCode,
      language: String(
        metadata.language_label ??
          (languageCode
            ? languageLabel(languageCode)
            : 'Language not specified')
      ),
      gender: titleize(String(metadata.gender ?? ''))
    };
  }
  if (service === 'kokoro') return kokoroDescriptor(voiceId);
  if (service === 'kobold_qwen' || service.includes('qwen')) {
    const preset = QWEN_PRESET_VOICES[voiceId.trim().toLowerCase()];
    if (preset) {
      // CustomVoice speakers are multilingual. Keep languageCode empty so a
      // preset remains selectable for every supported synthesis language;
      // `language` describes its native accent instead of filtering it out.
      return { id: voiceId, ...preset, languageCode: '' };
    }
  }
  if (service === 'magpie') {
    const [, locale = '', speaker = voiceId, emotion = ''] = voiceId.split('.');
    const languageCode = MAGPIE_LOCALES[locale.toUpperCase()] ?? '';
    const gender = ['Sofia', 'Aria'].includes(speaker)
      ? 'Female'
      : ['Jason', 'Leo', 'John Van Stan'].includes(speaker)
        ? 'Male'
        : '';
    return {
      id: voiceId,
      name: `${speaker}${emotion ? ` · ${emotion}` : ''}`,
      languageCode,
      language: LANGUAGE_LABELS[languageCode] ?? locale,
      gender
    };
  }
  if (service === 'voxtral') {
    const [prefix, genderCode] = voiceId.toLowerCase().split('_');
    const styles: Record<string, string> = {
      casual: 'Casual',
      cheerful: 'Cheerful',
      neutral: 'Neutral'
    };
    const presetLanguage = VOXTRAL_LANGUAGES.includes(prefix) ? prefix : '';
    const gender =
      genderCode === 'female' ? 'Female' : genderCode === 'male' ? 'Male' : '';
    const name = styles[prefix]
      ? `${styles[prefix]}${gender ? ` · ${gender}` : ''}`
      : presetLanguage
        ? `${languageLabel(presetLanguage)} · ${gender}`
        : titleize(voiceId);
    return {
      id: voiceId,
      name,
      languageCode: '',
      language: presetLanguage
        ? `${languageLabel(presetLanguage)} preset · Multilingual`
        : 'Multilingual',
      gender
    };
  }
  if (service === 'silero') {
    const knownPrefixes = new Set([
      'aze',
      'hye',
      'bak',
      'bel',
      'kat',
      'kbd',
      'kaz',
      'xal',
      'kir',
      'mdf',
      'tgk',
      'tat',
      'udm',
      'uzb',
      'ukr',
      'kjh',
      'chv',
      'erz',
      'sah',
      'ru',
      'en'
    ]);
    const prefix = voiceId.toLowerCase().split('_')[0];
    const languageCode = String(
      metadata?.language ?? (knownPrefixes.has(prefix) ? prefix : '')
    );
    const rawName = String(
      metadata?.display_name ??
        voiceId.replace(new RegExp(`^${prefix}_`, 'i'), '')
    );
    return {
      id: voiceId,
      name: titleize(rawName),
      languageCode,
      language:
        LANGUAGE_LABELS[languageCode] ?? (languageCode || 'Multilingual'),
      gender: ''
    };
  }
  if (service === 'elevenlabs') {
    const providerName = String(
      metadata?.name ?? metadata?.display_name ?? ''
    ).trim();
    const labels = metadata?.labels;
    const gender =
      labels && typeof labels === 'object' && 'gender' in labels
        ? String(labels.gender ?? '').trim()
        : '';
    return {
      id: voiceId,
      name: providerName || voiceId,
      // ElevenLabs voices are generally multilingual and its voice labels
      // describe accents/styles rather than a single synthesis language.
      // Keep them selectable for every language supported by the model.
      languageCode: '',
      language: 'Multilingual',
      gender
    };
  }
  if (['gemini', 'vertex_ai'].includes(service)) {
    const primaryLanguage = String(
      metadata?.language_code ?? metadata?.languageCode ?? ''
    ).trim();
    const languageCode =
      metadata?.multilingual === true ||
      ['mul', 'multilingual', 'und', 'auto'].includes(
        primaryLanguage.toLowerCase()
      )
        ? ''
        : canonicalLanguageTag(primaryLanguage);
    const label = languageCode ? languageLabel(languageCode) : 'Multilingual';
    const accent = String(metadata?.accent ?? '').trim();
    return {
      id: voiceId,
      name: String(metadata?.display_name ?? metadata?.name ?? voiceId),
      languageCode,
      language:
        accent && !label.toLowerCase().includes(accent.toLowerCase())
          ? `${label} · ${accent}`
          : label,
      gender: String(metadata?.gender ?? '')
    };
  }
  const locale = String(
    metadata?.locale ?? metadata?.language_code ?? metadata?.languageCode ?? ''
  ).trim();
  if (locale) {
    const rawName = voiceId.split(':')[0];
    const name = rawName.toLowerCase().startsWith(`${locale.toLowerCase()}-`)
      ? rawName.slice(locale.length + 1)
      : rawName;
    return {
      id: voiceId,
      name: String(metadata?.display_name ?? metadata?.name ?? name),
      languageCode:
        metadata?.multilingual === true ||
        ['mul', 'und', 'auto'].includes(locale.toLowerCase())
          ? ''
          : canonicalLanguageTag(locale),
      language: String(metadata?.language ?? languageLabel(locale)),
      gender: String(metadata?.gender ?? '')
    };
  }
  return {
    id: voiceId,
    name: String(metadata?.display_name ?? metadata?.name ?? titleize(voiceId)),
    languageCode: '',
    language:
      metadata?.multilingual === true
        ? 'Multilingual'
        : 'Language not specified',
    gender: String(metadata?.gender ?? '')
  };
}

export function defaultVoiceForLanguage(
  service:
    | Pick<
        TtsService,
        'default_voices_by_language' | 'default_voices' | 'default_voice'
      >
    | null
    | undefined,
  model: string,
  language: string
): string {
  const normalized = canonicalLanguageTag(language);
  const defaults = Object.fromEntries(
    Object.entries(service?.default_voices_by_language?.[model] ?? {}).map(
      ([key, value]) => [canonicalLanguageTag(key), value]
    )
  );
  const base = normalized.split('-')[0];
  const regional =
    normalized && !normalized.includes('-')
      ? Object.entries(defaults).find(
          ([key]) => key.split('-')[0] === normalized
        )?.[1]
      : undefined;
  return (
    defaults[normalized] ??
    defaults[base] ??
    regional ??
    service?.default_voices?.[model] ??
    service?.default_voice ??
    ''
  );
}

export function voiceSupportsLanguage(
  voice: VoiceDescriptor,
  language: string
) {
  return (
    !voice.languageCode ||
    !language ||
    languageMatches(voice.languageCode, language)
  );
}

export function languagesForService(
  _serviceId: string,
  _descriptors: VoiceDescriptor[],
  options: {
    modelId?: string;
    modelCatalog?: readonly ModelLanguageMetadata[];
  } = {}
) {
  const model = (options.modelCatalog ?? []).find(
    (item) =>
      String(item.id ?? '').trim() === String(options.modelId ?? '').trim()
  );
  // Voice accents and provider names cannot establish a model's coverage.
  const languages =
    model?.language_support?.languages ??
    model?.supported_languages ??
    model?.languages ??
    [];
  const codes = languages
    .map((entry) =>
      typeof entry === 'string'
        ? entry
        : String(entry.language_id ?? entry.code ?? entry.id ?? '')
    )
    .map((code) => canonicalLanguageTag(code))
    .filter(Boolean);
  return [...new Set(codes)].map((value) => ({
    value,
    label: languageLabel(value)
  }));
}
