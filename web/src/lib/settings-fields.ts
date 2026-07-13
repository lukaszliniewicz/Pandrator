export type SettingOption = { value: string | number; label: string };
export type NumberPresentation = { min?: number; max?: number; step?: number | 'any'; range?: boolean; suffix?: string };

const option = (value: string | number, label?: string): SettingOption => ({ value, label: label ?? String(value) });

export const LANGUAGE_OPTIONS: SettingOption[] = [
  option('auto', 'Automatic detection'), option('en', 'English'), option('pl', 'Polish'), option('de', 'German'),
  option('fr', 'French'), option('es', 'Spanish'), option('it', 'Italian'), option('pt', 'Portuguese'),
  option('pt-BR', 'Portuguese (Brazil)'), option('nl', 'Dutch'), option('sv', 'Swedish'), option('no', 'Norwegian'),
  option('da', 'Danish'), option('fi', 'Finnish'), option('cs', 'Czech'), option('sk', 'Slovak'),
  option('uk', 'Ukrainian'), option('ru', 'Russian'), option('bg', 'Bulgarian'), option('ro', 'Romanian'),
  option('hu', 'Hungarian'), option('el', 'Greek'), option('tr', 'Turkish'), option('ar', 'Arabic'),
  option('he', 'Hebrew'), option('fa', 'Persian'), option('hi', 'Hindi'), option('bn', 'Bengali'),
  option('ur', 'Urdu'), option('zh', 'Chinese'), option('ja', 'Japanese'), option('ko', 'Korean'),
  option('vi', 'Vietnamese'), option('th', 'Thai'), option('id', 'Indonesian'), option('ms', 'Malay'),
  option('ca', 'Catalan'), option('hr', 'Croatian'), option('sr', 'Serbian'), option('sl', 'Slovenian'),
  option('et', 'Estonian'), option('lv', 'Latvian'), option('lt', 'Lithuanian'), option('is', 'Icelandic'),
  option('cy', 'Welsh'), option('ga', 'Irish'), option('eu', 'Basque'), option('gl', 'Galician')
];

const CHOICES: Record<string, SettingOption[]> = {
  stt_engine: [option('whisper', 'Whisper large-v3'), option('parakeet', 'Parakeet 0.6B v3')],
  stt_model_quantization: [option('f16', 'FP16 (full precision)'), option('q8_0', 'Q8_0'), option('q5_0', 'Q5_0'), option('q4_k', 'Q4_K')],
  stt_compute_backend: [option('auto', 'Automatic'), option('cpu', 'CPU'), option('cuda', 'CUDA'), option('vulkan', 'Vulkan'), option('metal', 'Apple Metal')],
  stt_lid_backend: [option('whisper', 'Whisper language detection'), option('parakeet', 'Parakeet language detection')],
  parakeet_decoder: [option('tdt', 'TDT'), option('rnnt', 'RNNT')],
  crispasr_vad_model: [option('silero', 'Silero VAD')],
  max_lines: [option(1, '1 line'), option(2, '2 lines'), option(3, '3 lines')],
  backend: [option('llm', 'LLM provider'), option('deepl', 'DeepL')],
  f0_method: [option('rmvpe', 'RMVPE'), option('harvest', 'Harvest'), option('crepe', 'CREPE'), option('pm', 'PM')],
  pdf_ocr_mode: [option('auto', 'Automatic'), option('always', 'Always OCR'), option('never', 'Never OCR')],
  format: [option('wav', 'WAV'), option('mp3', 'MP3'), option('m4b', 'M4B audiobook'), option('flac', 'FLAC'), option('ogg', 'Ogg Vorbis')],
  bitrate: [option('96k', '96 kbps'), option('128k', '128 kbps'), option('160k', '160 kbps'), option('192k', '192 kbps'), option('256k', '256 kbps'), option('320k', '320 kbps')],
  audio_mode: [option('preserve', 'Preserve source audio'), option('mixed', 'Mix source and generated audio'), option('dubbing_only', 'Generated audio only')],
  subtitle_mode: [option('none', 'No subtitles'), option('soft', 'Inject soft subtitle tracks'), option('burn', 'Burn subtitles into video')],
  subtitle_selection: [option('translation', 'Translation only'), option('source', 'Source or corrected only'), option('both', 'Source and translation')],
  fishs2_latency: [option('low', 'Low latency'), option('balanced', 'Balanced'), option('normal', 'Quality')],
  service: ['XTTS', 'VoxCPM', 'FishS2', 'Voxtral', 'Kokoro', 'Magpie', 'Silero', 'Chatterbox', 'Qwen3 TTS', 'OpenAI', 'Google Gemini'].map((value) => option(value))
};

const LANGUAGE_KEYS = new Set(['language', 'stt_language', 'source_language', 'target_language', 'pdf_ocr_language']);
const MULTILINE_KEYS = new Set(['combined_prompt', 'first_prompt', 'second_prompt', 'third_prompt', 'whisper_prompt', 'instructions', 'glossary', 'stt_hotwords']);
const RANGE_KEYS = new Set(['crispasr_vad_threshold', 'index_rate', 'volume_envelope', 'protect', 'speed', 'top_p', 'fishs2_top_p', 'chatterbox_top_p', 'chatterbox_min_p', 'chatterbox_exaggeration', 'chatterbox_cfg_weight']);

export const GLOBAL_TTS_KEYS = new Set(['service', 'language', 'speed', 'max_attempts']);

export function optionsFor(section: string, key: string): SettingOption[] | null {
  if (LANGUAGE_KEYS.has(key)) {
    const values = section === 'output' ? LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto') : LANGUAGE_OPTIONS;
    return values;
  }
  return CHOICES[key] ?? null;
}

export function isMultiline(key: string): boolean { return MULTILINE_KEYS.has(key); }

export function numberPresentation(key: string): NumberPresentation {
  const meta: Record<string, NumberPresentation> = {
    crispasr_vad_threshold: { min: 0, max: 1, step: 0.05, range: true },
    index_rate: { min: 0, max: 1, step: 0.05, range: true },
    volume_envelope: { min: 0, max: 1, step: 0.05, range: true },
    protect: { min: 0, max: 0.5, step: 0.05, range: true },
    speed: { min: 0.25, max: 4, step: 0.05, range: true, suffix: '×' },
    top_p: { min: 0, max: 1, step: 0.05, range: true }, fishs2_top_p: { min: 0, max: 1, step: 0.05, range: true },
    chatterbox_top_p: { min: 0, max: 1, step: 0.05, range: true }, chatterbox_min_p: { min: 0, max: 1, step: 0.01, range: true },
    chatterbox_exaggeration: { min: 0, max: 1, step: 0.05, range: true }, chatterbox_cfg_weight: { min: 0, max: 1, step: 0.05, range: true },
    pitch: { min: -24, max: 24, step: 1 }, max_attempts: { min: 1, max: 20, step: 1 },
    stt_compute_device: { min: 0, step: 1 }, stt_threads: { min: 0, step: 1 }, stt_beam_size: { min: 1, step: 1 },
    max_lines: { min: 1, max: 3, step: 1 }, max_cps: { min: 1, step: 0.5 },
    temperature: { min: 0, max: 2, step: 0.05 }, fishs2_temperature: { min: 0, max: 2, step: 0.05 }, chatterbox_temperature: { min: 0, max: 2, step: 0.05 }
  };
  return meta[key] ?? { min: 0, step: 'any', range: RANGE_KEYS.has(key) };
}

const ACRONYMS: Record<string, string> = { stt: 'STT', tts: 'TTS', rvc: 'RVC', vad: 'VAD', llm: 'LLM', pdf: 'PDF', ocr: 'OCR', cps: 'CPS', dpi: 'DPI', url: 'URL', id: 'ID', fp16: 'FP16', gpt: 'GPT', wav: 'WAV', api: 'API', gpu: 'GPU', dtw: 'DTW', rnnt: 'RNNT', tdt: 'TDT', srt: 'SRT', ass: 'ASS', m4b: 'M4B' };

export function settingLabel(key: string): string {
  const labels: Record<string, string> = {
    llm_tts_optimization: 'Optimize each segment with an LLM',
    llm_tts_document_optimization: 'Optimize and review the document before generation',
    llm_tts_batch_size: 'Segments per inline JSON batch',
    llm_tts_document_batch_size: 'Segments per document JSON batch',
    tts_optimization_model: 'Speech optimization model',
    llm_processing_enabled: 'Enable LLM text processing',
    llm_multi_stage: 'Use divided prompts',
    llm_concurrent_calls: 'Concurrent LLM calls',
    combined_prompt: 'Single optimization prompt',
    first_prompt: 'First optimization prompt',
    second_prompt: 'Second optimization prompt',
    third_prompt: 'Third optimization prompt'
  };
  if (labels[key]) return labels[key];
  return key.split('_').map((word) => ACRONYMS[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1)).join(' ')
    .replace(/ Ms\b/g, ' (ms)').replace(/ Seconds\b/g, ' (seconds)');
}
