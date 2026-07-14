export type VoiceDescriptor = {
  id: string;
  name: string;
  languageCode: string;
  language: string;
  gender: string;
};

export const LANGUAGE_LABELS: Record<string, string> = {
  ar: 'Arabic', de: 'German', en: 'American English', 'en-gb': 'British English', es: 'Spanish',
  fr: 'French', hi: 'Hindi', it: 'Italian', ja: 'Japanese', ko: 'Korean', nl: 'Dutch',
  pl: 'Polish', pt: 'Portuguese', ru: 'Russian', tr: 'Turkish', cs: 'Czech', hu: 'Hungarian',
  vi: 'Vietnamese', 'zh-cn': 'Chinese (Simplified)', zh: 'Chinese',
  as: 'Assamese', aze: 'Azerbaijani', bak: 'Bashkir', bel: 'Belarusian', bn: 'Bengali',
  chv: 'Chuvash', erz: 'Erzya', gu: 'Gujarati', hye: 'Armenian', kat: 'Georgian',
  kaz: 'Kazakh', kbd: 'Kabardian-Cherkess', kir: 'Kyrgyz', kjh: 'Khakas', kn: 'Kannada',
  mdf: 'Moksha', ml: 'Malayalam', mni: 'Manipuri', raj: 'Rajasthani', sah: 'Yakut',
  ta: 'Tamil', tat: 'Tatar', te: 'Telugu', tgk: 'Tajik', udm: 'Udmurt',
  ukr: 'Ukrainian', uzb: 'Uzbek', xal: 'Kalmyk', indic: 'Indic languages', 'en-in': 'Indian English'
};

const KOKORO_PREFIX_LANGUAGES: Record<string, string> = {
  a: 'en', b: 'en-gb', d: 'de', e: 'es', f: 'fr', h: 'hi', i: 'it', j: 'ja', p: 'pt', z: 'zh-cn'
};

const KOKORO_LANGUAGES = ['en', 'en-gb', 'de', 'es', 'fr', 'hi', 'it', 'ja', 'pt', 'zh-cn'];
const QWEN_LANGUAGES = ['zh-cn', 'en', 'ja', 'ko', 'de', 'fr', 'ru', 'pt', 'es', 'it'];
const XTTS_LANGUAGES = ['en', 'es', 'fr', 'de', 'it', 'pt', 'pl', 'tr', 'ru', 'nl', 'cs', 'ar', 'zh-cn', 'ja', 'hu', 'ko', 'hi'];
const VOXTRAL_LANGUAGES = ['ar', 'en', 'de', 'es', 'fr', 'hi', 'it', 'nl', 'pt'];
const MAGPIE_LOCALES: Record<string, string> = {
  'EN-US': 'en', 'ES-US': 'es', 'FR-FR': 'fr', 'DE-DE': 'de', 'VI-VN': 'vi',
  'IT-IT': 'it', 'ZH-CN': 'zh', 'HI-IN': 'hi', 'JA-JP': 'ja'
};

const titleize = (value: string) => value.split(/[_-]+/).filter(Boolean).map((token) => token.charAt(0).toUpperCase() + token.slice(1)).join(' ');

function kokoroDescriptor(id: string): VoiceDescriptor {
  const first = id.split('+')[0].trim().replace(/\(\s*\d+(?:\.\d+)?\s*\)$/, '');
  const match = first.match(/^([abdefhijpz])([fm])_(.+)$/i);
  if (match) {
    const languageCode = KOKORO_PREFIX_LANGUAGES[match[1].toLowerCase()] ?? '';
    const gender = match[2].toLowerCase() === 'f' ? 'Female' : 'Male';
    const names = id.split('+').map((part) => {
      const clean = part.trim().replace(/\(\s*\d+(?:\.\d+)?\s*\)$/, '');
      const component = clean.match(/^[abdefhijpz][fm]_(.+)$/i);
      return titleize(component?.[1] ?? clean);
    });
    return { id, name: names.length > 1 ? `Blend: ${names.join(' + ')}` : names[0], languageCode, language: LANGUAGE_LABELS[languageCode] ?? languageCode, gender };
  }
  if (first.toLowerCase() === 'martin') return { id, name: 'Martin', languageCode: 'de', language: 'German', gender: 'Male' };
  return { id, name: titleize(first), languageCode: '', language: 'Multilingual', gender: '' };
}

export function describeVoice(serviceId: string, voiceId: string, metadata?: Record<string, any>): VoiceDescriptor {
  const service = serviceId.toLowerCase().replaceAll('-', '_');
  if (service === 'kokoro') return kokoroDescriptor(voiceId);
  if (service === 'magpie') {
    const [, locale = '', speaker = voiceId, emotion = ''] = voiceId.split('.');
    const languageCode = MAGPIE_LOCALES[locale.toUpperCase()] ?? '';
    const gender = ['Sofia', 'Aria'].includes(speaker) ? 'Female' : ['Jason', 'Leo', 'John Van Stan'].includes(speaker) ? 'Male' : '';
    return { id: voiceId, name: `${speaker}${emotion ? ` · ${emotion}` : ''}`, languageCode, language: LANGUAGE_LABELS[languageCode] ?? locale, gender };
  }
  if (service === 'voxtral') {
    const [prefix, genderCode] = voiceId.toLowerCase().split('_');
    const styles: Record<string, string> = { casual: 'Casual', cheerful: 'Cheerful', neutral: 'Neutral' };
    const languageCode = styles[prefix] ? 'en' : VOXTRAL_LANGUAGES.includes(prefix) ? prefix : '';
    const gender = genderCode === 'female' ? 'Female' : genderCode === 'male' ? 'Male' : '';
    const name = styles[prefix] ? `${styles[prefix]}${gender ? ` ${gender}` : ''}` : languageCode ? `Standard${gender ? ` ${gender}` : ''}` : titleize(voiceId);
    return { id: voiceId, name, languageCode, language: LANGUAGE_LABELS[languageCode] ?? 'Multilingual', gender };
  }
  if (service === 'silero') {
    const knownPrefixes = new Set(['aze', 'hye', 'bak', 'bel', 'kat', 'kbd', 'kaz', 'xal', 'kir', 'mdf', 'tgk', 'tat', 'udm', 'uzb', 'ukr', 'kjh', 'chv', 'erz', 'sah', 'ru', 'en']);
    const prefix = voiceId.toLowerCase().split('_')[0];
    const languageCode = String(metadata?.language ?? (knownPrefixes.has(prefix) ? prefix : ''));
    const rawName = String(metadata?.display_name ?? voiceId.replace(new RegExp(`^${prefix}_`, 'i'), ''));
    return {
      id: voiceId,
      name: titleize(rawName),
      languageCode,
      language: LANGUAGE_LABELS[languageCode] ?? (languageCode || 'Multilingual'),
      gender: ''
    };
  }
  return { id: voiceId, name: titleize(voiceId), languageCode: '', language: 'Multilingual', gender: '' };
}

export function languagesForService(serviceId: string, descriptors: VoiceDescriptor[]) {
  const service = serviceId.toLowerCase().replaceAll('-', '_');
  const codes = service === 'kokoro' ? KOKORO_LANGUAGES
    : service === 'kobold_qwen' || service.includes('qwen') ? QWEN_LANGUAGES
      : ['xtts', 'fishs2'].includes(service) ? XTTS_LANGUAGES
      : service === 'voxtral' ? VOXTRAL_LANGUAGES
        : service === 'silero' ? Array.from(new Set(descriptors.map((voice) => voice.languageCode).filter(Boolean)))
        : service === 'magpie' ? Object.values(MAGPIE_LOCALES)
          : Array.from(new Set(descriptors.map((voice) => voice.languageCode).filter(Boolean)));
  return Array.from(new Set(codes)).map((code) => ({ value: code, label: LANGUAGE_LABELS[code] ?? code }));
}
