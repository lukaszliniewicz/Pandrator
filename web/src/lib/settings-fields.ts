export type SettingOption = { value: string | number; label: string };
export type NumberPresentation = {
  min?: number;
  max?: number;
  step?: number | 'any';
  range?: boolean;
  suffix?: string;
};

const option = (value: string | number, label?: string): SettingOption => ({
  value,
  label: label ?? String(value)
});

export const LANGUAGE_OPTIONS: SettingOption[] = [
  option('auto', 'Automatic detection'),
  option('en', 'English'),
  option('pl', 'Polish'),
  option('de', 'German'),
  option('fr', 'French'),
  option('es', 'Spanish'),
  option('it', 'Italian'),
  option('pt', 'Portuguese'),
  option('pt-BR', 'Portuguese (Brazil)'),
  option('nl', 'Dutch'),
  option('sv', 'Swedish'),
  option('no', 'Norwegian'),
  option('da', 'Danish'),
  option('fi', 'Finnish'),
  option('cs', 'Czech'),
  option('sk', 'Slovak'),
  option('uk', 'Ukrainian'),
  option('ru', 'Russian'),
  option('bg', 'Bulgarian'),
  option('ro', 'Romanian'),
  option('hu', 'Hungarian'),
  option('el', 'Greek'),
  option('tr', 'Turkish'),
  option('ar', 'Arabic'),
  option('he', 'Hebrew'),
  option('fa', 'Persian'),
  option('hi', 'Hindi'),
  option('bn', 'Bengali'),
  option('ur', 'Urdu'),
  option('zh', 'Chinese'),
  option('ja', 'Japanese'),
  option('ko', 'Korean'),
  option('vi', 'Vietnamese'),
  option('th', 'Thai'),
  option('id', 'Indonesian'),
  option('ms', 'Malay'),
  option('ca', 'Catalan'),
  option('hr', 'Croatian'),
  option('sr', 'Serbian'),
  option('sl', 'Slovenian'),
  option('et', 'Estonian'),
  option('lv', 'Latvian'),
  option('lt', 'Lithuanian'),
  option('is', 'Icelandic'),
  option('cy', 'Welsh'),
  option('ga', 'Irish'),
  option('eu', 'Basque'),
  option('gl', 'Galician')
];

const CHOICES: Record<string, SettingOption[]> = {
  audio_verification_mode: [
    option('off', 'Off'),
    option('signal', 'Flag suspicious raw audio')
  ],
  stt_engine: [
    option('whisper', 'Whisper large-v3'),
    option('parakeet', 'Parakeet 0.6B v3'),
    option('moss', 'MOSS Transcribe-Diarize 0.9B'),
    option('azure_mai_transcribe_1_5', 'Azure Speech · MAI-Transcribe-1.5')
  ],
  stt_transcribe_style: [
    option('readability', 'Readable transcript'),
    option('verbatim', 'Verbatim · preserve fillers')
  ],
  azure_speech_output_format: [
    option('audio-24khz-160kbitrate-mono-mp3', 'MP3 · 24 kHz · 160 kbps'),
    option('riff-24khz-16bit-mono-pcm', 'WAV · 24 kHz · 16-bit PCM')
  ],
  stt_model_quantization: [
    option('f16', 'FP16 (full precision)'),
    option('q8_0', 'Q8_0'),
    option('q5_0', 'Q5_0'),
    option('q4_k', 'Q4_K')
  ],
  stt_compute_backend: [
    option('auto', 'Automatic'),
    option('cpu', 'CPU'),
    option('cuda', 'CUDA'),
    option('vulkan', 'Vulkan'),
    option('metal', 'Apple Metal')
  ],
  stt_lid_backend: [
    option('whisper', 'Whisper language detection'),
    option('parakeet', 'Parakeet language detection')
  ],
  parakeet_decoder: [option('tdt', 'TDT'), option('rnnt', 'RNNT')],
  crispasr_vad_model: [option('silero', 'Silero VAD')],
  max_lines: [option(1, '1 line'), option(2, '2 lines'), option(3, '3 lines')],
  backend: [option('llm', 'LLM provider'), option('deepl', 'DeepL')],
  reasoning_effort: [
    option('', 'Use model default'),
    option('minimal', 'Minimal · fastest'),
    option('low', 'Low · economical'),
    option('medium', 'Medium · balanced'),
    option('high', 'High · strongest')
  ],
  timing_context_mode: [
    option('full', 'Full timing · best quality'),
    option('overlap_only', 'Overlap only · fewer tokens'),
    option('none', 'No timing context')
  ],
  speech_optimization_mode: [
    option('guarded', 'Guarded speech plan (recommended)'),
    option('flexible', 'Flexible contextual rewrite')
  ],
  web_research_provider: [option('jina', 'Jina Reader')],
  web_research_mode: [
    option('global', 'Research the whole stage once'),
    option('per_chunk', 'Research each processing chunk')
  ],
  f0_method: [
    option('rmvpe', 'RMVPE'),
    option('harvest', 'Harvest'),
    option('crepe', 'CREPE'),
    option('pm', 'PM')
  ],
  pdf_ocr_mode: [
    option('auto', 'Automatic'),
    option('force', 'Always OCR'),
    option('off', 'Never OCR')
  ],
  format: [
    option('wav', 'WAV'),
    option('mp3', 'MP3'),
    option('m4b', 'M4B audiobook'),
    option('flac', 'FLAC'),
    option('opus', 'Opus')
  ],
  bitrate: [
    option('96k', '96 kbps'),
    option('128k', '128 kbps'),
    option('160k', '160 kbps'),
    option('192k', '192 kbps'),
    option('256k', '256 kbps'),
    option('320k', '320 kbps')
  ],
  burn_audio_bitrate: [
    option('96k', '96 kbps'),
    option('128k', '128 kbps'),
    option('160k', '160 kbps'),
    option('192k', '192 kbps'),
    option('256k', '256 kbps'),
    option('320k', '320 kbps')
  ],
  export_mode: [
    option('media', 'Video / media'),
    option('subtitles', 'Subtitles only'),
    option('text', 'Concatenated text only')
  ],
  audio_mode: [
    option('preserve', 'Preserve source audio'),
    option('mixed', 'Mix source and generated audio'),
    option('dubbing_only', 'Generated audio only')
  ],
  mix_ducking: [
    option('very_strong', 'Very strong (voice priority)'),
    option('strong', 'Strong (recommended)'),
    option('balanced', 'Balanced'),
    option('gentle', 'Gentle'),
    option('off', 'Off')
  ],
  subtitle_mode: [
    option('none', 'No subtitles'),
    option('soft', 'Inject soft subtitle tracks'),
    option('burned', 'Burn subtitles into video')
  ],
  subtitle_selection: [
    option('translation', 'Translation only'),
    option('source', 'Source or corrected only'),
    option('dual', 'Source and translation')
  ],
  subtitle_format: [
    option('srt', 'SubRip (.srt)'),
    option('vtt', 'WebVTT (.vtt)')
  ],
  burn_video_encoder: [
    option('libx264', 'H.264 software'),
    option('libx265', 'H.265 software'),
    option('h264_vaapi', 'H.264 VA-API'),
    option('hevc_vaapi', 'H.265 VA-API'),
    option('h264_amf', 'H.264 AMD AMF'),
    option('hevc_amf', 'H.265 AMD AMF'),
    option('h264_nvenc', 'H.264 NVIDIA NVENC'),
    option('hevc_nvenc', 'H.265 NVIDIA NVENC'),
    option('h264_qsv', 'H.264 Intel Quick Sync'),
    option('hevc_qsv', 'H.265 Intel Quick Sync')
  ],
  burn_video_resolution: [
    option('source', 'Source resolution'),
    option('360p'),
    option('480p'),
    option('720p', '720p (HD)'),
    option('1080p', '1080p (Full HD)'),
    option('1440p', '1440p (QHD)'),
    option('2160p', '2160p (4K)')
  ],
  burn_video_speed: [
    option('fast', 'Fast'),
    option('balanced', 'Balanced'),
    option('quality', 'Quality')
  ],
  burn_audio_codec: [
    option('copy', 'Copy without transcoding'),
    option('aac', 'Transcode to AAC')
  ],
  fishs2_latency: [
    option('low', 'Low latency'),
    option('balanced', 'Balanced'),
    option('normal', 'Quality')
  ],
  silero_stress_mode: [
    option('auto', 'Automatic stress'),
    option('manual', 'Use supplied stress marks'),
    option('off', 'Do not add stress marks')
  ],
  silero_sample_rate: [
    option(8000, '8 kHz'),
    option(24000, '24 kHz'),
    option(48000, '48 kHz')
  ],
  service: [
    'XTTS',
    'VoxCPM',
    'FishS2',
    'Voxtral',
    'Kokoro',
    'Magpie',
    'Silero',
    'Chatterbox',
    'Qwen3 TTS',
    'OpenAI',
    'Google Gemini'
  ].map((value) => option(value))
};

const LANGUAGE_KEYS = new Set([
  'language',
  'stt_language',
  'source_language',
  'target_language',
  'pdf_ocr_language'
]);
const MULTILINE_KEYS = new Set([
  'combined_prompt',
  'first_prompt',
  'second_prompt',
  'third_prompt',
  'whisper_prompt',
  'instructions',
  'generation_prompt',
  'glossary',
  'stt_hotwords',
  'web_research_preferred_domains',
  'web_research_blocked_domains'
]);
const RANGE_KEYS = new Set([
  'crispasr_vad_threshold',
  'index_rate',
  'volume_envelope',
  'protect',
  'speed',
  'top_p',
  'fishs2_top_p',
  'chatterbox_top_p',
  'chatterbox_min_p',
  'chatterbox_exaggeration',
  'chatterbox_cfg_weight',
  'web_research_context_fraction'
]);

export const GLOBAL_TTS_KEYS = new Set([
  'service',
  'language',
  'speed',
  'max_attempts'
]);

const SETTING_ORDER: Record<string, string[]> = {
  text: [
    'enable_sentence_splitting',
    'max_sentence_length',
    'enable_sentence_appending',
    'disable_paragraph_detection',
    'remove_diacritics',
    'remove_quotation_marks',
    'remove_footnotes',
    'filter_citations',
    'enable_nemo_normalization',
    'normalize_all_caps',
    'apply_reviewed_pronunciations',
    'llm_tts_document_optimization',
    'llm_tts_optimization',
    'speech_optimization_mode',
    'speech_plan_min_retention',
    'speech_plan_save_proposals',
    'tts_optimization_model',
    'llm_concurrent_calls',
    'llm_tts_document_batch_size',
    'llm_tts_batch_size',
    'llm_multi_stage',
    'combined_prompt',
    'first_prompt',
    'second_prompt',
    'third_prompt'
  ],
  stt: [
    'stt_engine',
    'stt_language',
    'stt_transcribe_style',
    'stt_model_quantization',
    'stt_compute_backend',
    'stt_compute_device',
    'stt_threads',
    'stt_chunk_seconds',
    'stt_chunk_overlap_seconds',
    'stt_beam_size',
    'stt_lid_backend',
    'stt_hotwords',
    'whisper_prompt',
    'parakeet_decoder',
    'moss_max_chunk_seconds',
    'moss_chunk_overlap_seconds',
    'crispasr_vad_enabled',
    'moss_vad_enabled',
    'crispasr_vad_model',
    'crispasr_vad_threshold',
    'crispasr_vad_min_speech_ms',
    'crispasr_vad_min_silence_ms',
    'crispasr_vad_max_speech_seconds',
    'crispasr_vad_speech_pad_ms',
    'moss_ctc_alignment_enabled',
    'moss_ctc_aligner_model',
    'moss_ctc_padding_seconds',
    'diarization_enabled'
  ],
  subtitles: [
    'max_lines',
    'max_chars_per_line',
    'max_cps',
    'min_duration_ms',
    'max_duration_ms',
    'min_gap_ms',
    'phrase_gap_ms',
    'hard_gap_ms',
    'sentence_boundary_threshold',
    'merge_threshold_ms'
  ],
  correction: [
    'enabled',
    'model_name',
    'reasoning_effort',
    'instructions',
    'char_limit',
    'max_segments_per_batch',
    'llm_concurrent_calls',
    'timing_context_mode',
    'substantial_gap_ms',
    'context_before',
    'context_after',
    'no_remove_subtitles',
    'request_timeout_seconds',
    'web_research_enabled',
    'web_research_provider',
    'web_research_model_name',
    'web_research_mode',
    'web_research_context_fraction',
    'web_research_language',
    'web_research_max_searches',
    'web_research_max_extractions',
    'web_research_preferred_domains',
    'web_research_blocked_domains',
    'web_research_max_iterations',
    'web_research_timeout_seconds',
    'web_research_source_chars',
    'web_research_result_chars'
  ],
  translation: [
    'enabled',
    'backend',
    'source_language',
    'target_language',
    'model_name',
    'reasoning_effort',
    'instructions',
    'glossary_enabled',
    'glossary',
    'context',
    'char_limit',
    'max_segments_per_batch',
    'llm_concurrent_calls',
    'timing_context_mode',
    'substantial_gap_ms',
    'context_before',
    'context_after',
    'no_remove_subtitles',
    'request_timeout_seconds',
    'web_research_enabled',
    'web_research_provider',
    'web_research_model_name',
    'web_research_mode',
    'web_research_context_fraction',
    'web_research_language',
    'web_research_max_searches',
    'web_research_max_extractions',
    'web_research_preferred_domains',
    'web_research_blocked_domains',
    'web_research_max_iterations',
    'web_research_timeout_seconds',
    'web_research_source_chars',
    'web_research_result_chars'
  ],
  tts: ['service', 'language', 'speed', 'max_attempts'],
  audio: [
    'audio_verification_mode',
    'sentence_silence_ms',
    'paragraph_silence_ms',
    'fade_enabled',
    'fade_in_ms',
    'fade_out_ms',
    'synchronization_delay_ms',
    'synchronization_speed',
    'synchronization_sentence_gap_ms'
  ],
  rvc: [
    'enabled',
    'model',
    'pitch',
    'f0_method',
    'filter_radius',
    'index_rate',
    'volume_envelope',
    'protect'
  ],
  source_cleaning: [
    'agentic',
    'max_iterations',
    'phase_max_iterations',
    'request_timeout_seconds',
    'pdf_ocr_mode',
    'pdf_ocr_language',
    'pdf_ocr_dpi',
    'pdf_remove_toc',
    'pdf_remove_repeated_marginals',
    'remove_footnotes',
    'filter_citations'
  ],
  output: [
    'export_mode',
    'format',
    'bitrate',
    'audio_mode',
    'mix_source_gain_db',
    'mix_voice_gain_db',
    'mix_voice_lufs',
    'mix_ducking',
    'mix_attack_ms',
    'mix_release_ms',
    'mix_audio_bitrate',
    'subtitle_mode',
    'subtitle_selection',
    'subtitle_format',
    'video_transcode',
    'burn_video_encoder',
    'burn_video_resolution',
    'burn_video_quality',
    'burn_video_speed',
    'burn_audio_codec',
    'burn_audio_bitrate',
    'title',
    'artist',
    'album',
    'genre',
    'language'
  ]
};

export function compareSettingOrder(
  section: string,
  left: string,
  right: string
): number {
  const order = SETTING_ORDER[section] ?? [];
  const leftIndex = order.indexOf(left);
  const rightIndex = order.indexOf(right);
  return (
    (leftIndex < 0 ? Number.MAX_SAFE_INTEGER : leftIndex) -
      (rightIndex < 0 ? Number.MAX_SAFE_INTEGER : rightIndex) ||
    left.localeCompare(right)
  );
}

const STT_LOCAL_ENGINES = new Set(['whisper', 'parakeet', 'moss']);
const STT_LOCAL_KEYS = new Set([
  'stt_model_quantization',
  'stt_compute_backend',
  'stt_threads',
  'stt_chunk_seconds',
  'stt_lid_backend'
]);
const STT_NON_MOSS_LOCAL_KEYS = new Set([
  'stt_chunk_overlap_seconds',
  'stt_beam_size',
  'diarization_enabled'
]);
const STT_VAD_DETAIL_KEYS = new Set([
  'crispasr_vad_model',
  'crispasr_vad_threshold',
  'crispasr_vad_min_speech_ms',
  'crispasr_vad_min_silence_ms',
  'crispasr_vad_max_speech_seconds',
  'crispasr_vad_speech_pad_ms'
]);
const MOSS_CTC_DETAIL_KEYS = new Set([
  'moss_ctc_aligner_model',
  'moss_ctc_padding_seconds'
]);
const DISABLED_STAGE_KEYS = new Set(['correction', 'translation', 'rvc']);
const DEEPL_HIDDEN_KEYS = new Set([
  'model_name',
  'reasoning_effort',
  'llm_concurrent_calls',
  'timing_context_mode',
  'substantial_gap_ms',
  'instructions',
  'glossary',
  'glossary_enabled',
  'context',
  'context_before',
  'context_after',
  'char_limit',
  'max_segments_per_batch',
  'no_remove_subtitles',
  'request_timeout_seconds'
]);
const OUTPUT_VIDEO_KEYS = new Set([
  'burn_video_encoder',
  'burn_video_resolution',
  'burn_video_quality',
  'burn_video_speed',
  'burn_audio_codec',
  'burn_audio_bitrate'
]);
const OUTPUT_MIX_KEYS = new Set([
  'mix_source_gain_db',
  'mix_voice_gain_db',
  'mix_voice_lufs',
  'mix_ducking',
  'mix_attack_ms',
  'mix_release_ms',
  'mix_audio_bitrate'
]);

/**
 * Return whether a setting currently affects the selected pipeline branch.
 * Values remain stored while hidden, so turning a feature back on restores its
 * previously chosen defaults.
 */
export function settingApplies(
  section: string,
  key: string,
  settings: Record<string, unknown>
): boolean {
  const enabled = (name: string, fallback = false) =>
    Object.prototype.hasOwnProperty.call(settings, name)
      ? Boolean(settings[name])
      : fallback;
  const selected = (name: string, fallback = '') =>
    String(settings[name] ?? fallback)
      .trim()
      .toLowerCase();

  if (
    DISABLED_STAGE_KEYS.has(section) &&
    key !== 'enabled' &&
    !enabled('enabled')
  )
    return false;

  if (section === 'correction' && key.startsWith('web_research_'))
    return key === 'web_research_enabled' || enabled('web_research_enabled');

  if (section === 'translation') {
    const deepL = selected('backend', 'llm') === 'deepl';
    if (
      deepL &&
      (DEEPL_HIDDEN_KEYS.has(key) || key.startsWith('web_research_'))
    )
      return false;
    if (key.startsWith('web_research_'))
      return key === 'web_research_enabled' || enabled('web_research_enabled');
  }

  if (section === 'rvc') return key === 'enabled' || enabled('enabled');

  if (section === 'audio' && ['fade_in_ms', 'fade_out_ms'].includes(key))
    return enabled('fade_enabled');

  if (section === 'text') {
    if (key === 'llm_processing_enabled') return false;
    const immediate = enabled('llm_tts_optimization');
    const document = enabled('llm_tts_document_optimization');
    const planning = immediate || document;
    if (key === 'llm_tts_batch_size') return immediate;
    if (key === 'llm_tts_document_batch_size') return document;
    if (
      key.startsWith('speech_') ||
      [
        'tts_optimization_model',
        'llm_concurrent_calls',
        'llm_multi_stage',
        'combined_prompt',
        'first_prompt',
        'second_prompt',
        'third_prompt'
      ].includes(key)
    ) {
      if (!planning) return false;
      if (key === 'speech_plan_min_retention')
        return selected('speech_optimization_mode', 'guarded') === 'flexible';
      if (['first_prompt', 'second_prompt', 'third_prompt'].includes(key))
        return enabled('llm_multi_stage');
      if (key === 'combined_prompt') return !enabled('llm_multi_stage');
    }
  }

  if (section === 'source_cleaning') {
    if (
      [
        'max_iterations',
        'phase_max_iterations',
        'request_timeout_seconds'
      ].includes(key)
    )
      return enabled('agentic');
    if (['pdf_ocr_language', 'pdf_ocr_dpi'].includes(key))
      return selected('pdf_ocr_mode', 'auto') !== 'off';
  }

  if (section === 'subtitles' && key === 'boundary_correction_enabled')
    return false;

  if (section === 'output') {
    const exportMode = selected('export_mode', 'media');
    if (exportMode === 'subtitles')
      return [
        'export_mode',
        'subtitle_selection',
        'subtitle_format',
        'language'
      ].includes(key);
    if (exportMode === 'text')
      return ['export_mode', 'subtitle_selection', 'language'].includes(key);
    const subtitleMode = selected('subtitle_mode', 'none');
    const transcodesVideo =
      enabled('video_transcode') || subtitleMode === 'burned';
    if (['subtitle_selection', 'subtitle_format'].includes(key))
      return subtitleMode !== 'none';
    if (OUTPUT_VIDEO_KEYS.has(key)) {
      if (!transcodesVideo) return false;
      if (key === 'burn_audio_bitrate')
        return selected('burn_audio_codec', 'copy') === 'aac';
      return true;
    }
    if (OUTPUT_MIX_KEYS.has(key))
      return selected('audio_mode', 'mixed') === 'mixed';
    if (key === 'bitrate')
      return ['mp3', 'm4b', 'opus'].includes(selected('format', 'wav'));
  }

  if (section !== 'stt') return true;

  const engine = selected('stt_engine', 'whisper');
  const local = STT_LOCAL_ENGINES.has(engine);
  const moss = engine === 'moss';
  const nonMossLocal = engine === 'whisper' || engine === 'parakeet';

  if (STT_LOCAL_KEYS.has(key)) return local;
  if (STT_NON_MOSS_LOCAL_KEYS.has(key)) return nonMossLocal;
  if (key === 'stt_compute_device')
    return (
      local &&
      !['auto', 'cpu'].includes(selected('stt_compute_backend', 'auto'))
    );
  if (key === 'whisper_prompt') return engine === 'whisper';
  if (key === 'stt_transcribe_style')
    return engine === 'azure_mai_transcribe_1_5';
  if (key === 'parakeet_decoder') return engine === 'parakeet';
  if (key === 'moss_max_chunk_seconds')
    return moss && Number(settings.stt_chunk_seconds ?? 0) <= 0;
  if (key.startsWith('moss_')) {
    if (!moss) return false;
    if (MOSS_CTC_DETAIL_KEYS.has(key))
      return enabled('moss_ctc_alignment_enabled', true);
    return true;
  }
  if (key === 'crispasr_vad_enabled') return nonMossLocal;
  if (STT_VAD_DETAIL_KEYS.has(key))
    return (
      local &&
      (moss
        ? enabled('moss_vad_enabled')
        : enabled('crispasr_vad_enabled', true))
    );
  return true;
}

export function optionsFor(
  section: string,
  key: string
): SettingOption[] | null {
  if (LANGUAGE_KEYS.has(key)) {
    const values =
      section === 'output'
        ? LANGUAGE_OPTIONS.filter((item) => item.value !== 'auto')
        : LANGUAGE_OPTIONS;
    return values;
  }
  return CHOICES[key] ?? null;
}

export function isMultiline(key: string): boolean {
  return MULTILINE_KEYS.has(key);
}

export function numberPresentation(key: string): NumberPresentation {
  const meta: Record<string, NumberPresentation> = {
    crispasr_vad_threshold: { min: 0, max: 1, step: 0.05, range: true },
    index_rate: { min: 0, max: 1, step: 0.05, range: true },
    volume_envelope: { min: 0, max: 1, step: 0.05, range: true },
    protect: { min: 0, max: 0.5, step: 0.05, range: true },
    speed: { min: 0.25, max: 4, step: 0.05, range: true, suffix: '×' },
    top_p: { min: 0, max: 1, step: 0.05, range: true },
    fishs2_top_p: { min: 0, max: 1, step: 0.05, range: true },
    chatterbox_top_p: { min: 0, max: 1, step: 0.05, range: true },
    chatterbox_min_p: { min: 0, max: 1, step: 0.01, range: true },
    chatterbox_exaggeration: { min: 0, max: 1, step: 0.05, range: true },
    chatterbox_cfg_weight: { min: 0, max: 1, step: 0.05, range: true },
    web_research_context_fraction: {
      min: 0.1,
      max: 0.8,
      step: 0.05,
      range: true
    },
    speech_plan_min_retention: { min: 0.75, max: 1, step: 0.01, range: true },
    pitch: { min: -24, max: 24, step: 1 },
    max_attempts: { min: 1, max: 20, step: 1 },
    burn_video_quality: { min: 0, max: 51, step: 1 },
    stt_compute_device: { min: 0, step: 1 },
    stt_threads: { min: 0, step: 1 },
    stt_beam_size: { min: 1, step: 1 },
    pdf_ocr_dpi: { min: 120, max: 400, step: 1 },
    char_limit: { min: 1, max: 100000, step: 100 },
    max_segments_per_batch: { min: 1, max: 500, step: 1 },
    context_before: { min: 0, max: 20, step: 1 },
    context_after: { min: 0, max: 20, step: 1 },
    substantial_gap_ms: { min: 0, max: 60000, step: 100 },
    moss_max_chunk_seconds: { min: 30, max: 120, step: 1 },
    moss_ctc_padding_seconds: { min: 0, max: 2, step: 0.1 },
    max_lines: { min: 1, max: 3, step: 1 },
    max_cps: { min: 1, step: 0.5 },
    synchronization_delay_ms: { min: 0, max: 10000, step: 50 },
    synchronization_speed: { min: 1, max: 4, step: 0.01 },
    synchronization_sentence_gap_ms: { min: 0, max: 5000, step: 10 },
    mix_source_gain_db: { min: -60, max: 12, step: 0.5 },
    mix_voice_gain_db: { min: -30, max: 12, step: 0.5 },
    mix_voice_lufs: { min: -30, max: -8, step: 0.5 },
    mix_attack_ms: { min: 1, max: 2000, step: 1 },
    mix_release_ms: { min: 10, max: 5000, step: 10 },
    temperature: { min: 0, max: 2, step: 0.05 },
    fishs2_temperature: { min: 0, max: 2, step: 0.05 },
    chatterbox_temperature: { min: 0, max: 2, step: 0.05 },
    azure_speech_style_degree: { min: 0.01, max: 2, step: 0.01 }
  };
  return meta[key] ?? { min: 0, step: 'any', range: RANGE_KEYS.has(key) };
}

const ACRONYMS: Record<string, string> = {
  stt: 'STT',
  tts: 'TTS',
  rvc: 'RVC',
  vad: 'VAD',
  llm: 'LLM',
  pdf: 'PDF',
  ocr: 'OCR',
  cps: 'CPS',
  dpi: 'DPI',
  url: 'URL',
  id: 'ID',
  fp16: 'FP16',
  gpt: 'GPT',
  wav: 'WAV',
  api: 'API',
  gpu: 'GPU',
  dtw: 'DTW',
  rnnt: 'RNNT',
  tdt: 'TDT',
  srt: 'SRT',
  ass: 'ASS',
  m4b: 'M4B'
};

export function settingLabel(key: string): string {
  const labels: Record<string, string> = {
    audio_verification_mode: 'Generated-audio verification',
    llm_tts_optimization: 'Optimize each segment with an LLM',
    llm_tts_document_optimization:
      'Optimize and review the document before generation',
    apply_reviewed_pronunciations:
      'Apply reviewed pronunciation-library overrides',
    llm_tts_batch_size: 'Units per generation-time model request',
    llm_tts_document_batch_size: 'Units per document model request',
    tts_batch_size: 'Segments per streaming speech batch',
    tts_optimization_model: 'Speech optimization model',
    reasoning_effort: 'Reasoning level',
    speech_optimization_mode: 'Speech planning mode',
    speech_plan_min_retention: 'Minimum flexible-text retention',
    speech_plan_save_proposals: 'Save new pronunciations for review',
    web_research_enabled: 'Ground uncertain terms with web research',
    web_research_provider: 'Research provider',
    web_research_model_name: 'Researcher model',
    web_research_mode: 'Research mode',
    web_research_context_fraction: 'Maximum context used for research',
    web_research_language: 'Preferred research language',
    web_research_max_searches: 'Maximum searches per stage',
    web_research_max_extractions: 'Maximum page extractions per stage',
    web_research_preferred_domains: 'Preferred domains (optional)',
    web_research_blocked_domains: 'Blocked domains',
    web_research_max_iterations: 'Maximum research-agent turns',
    web_research_timeout_seconds: 'Research request timeout (seconds)',
    web_research_source_chars: 'Maximum source characters for research',
    web_research_result_chars: 'Maximum characters per tool result',
    llm_processing_enabled: 'Enable LLM text processing',
    agentic: 'Use LLM-assisted source cleaning',
    max_iterations: 'Maximum source-cleaning agent turns',
    fade_enabled: 'Fade generated audio edges',
    fade_in_ms: 'Fade-in duration (ms)',
    fade_out_ms: 'Fade-out duration (ms)',
    llm_multi_stage: 'Use divided prompts',
    llm_concurrent_calls: 'Concurrent LLM requests',
    timing_context_mode: 'Cue timing context',
    substantial_gap_ms: 'Substantial audible pause (ms)',
    char_limit: 'Maximum batch characters',
    max_segments_per_batch: 'Maximum cues per batch',
    context_before: 'Previous output cues for continuity',
    context_after: 'Following source cues for continuity',
    combined_prompt: 'Single optimization prompt',
    first_prompt: 'First optimization prompt',
    second_prompt: 'Second optimization prompt',
    third_prompt: 'Third optimization prompt',
    generation_prompt: 'Speech direction',
    azure_speech_style: 'Azure speaking style (optional)',
    azure_speech_style_degree: 'Azure style intensity',
    azure_speech_output_format: 'Azure audio format',
    silero_stress_mode: 'Stress handling',
    silero_sample_rate: 'Sample rate',
    synchronization_delay_ms: 'Maximum voiceover start delay',
    synchronization_speed: 'Maximum synchronization speed-up',
    synchronization_sentence_gap_ms: 'Generated sentence gap',
    mix_source_gain_db: 'Source soundtrack level (dB)',
    mix_voice_gain_db: 'Voiceover level (dB)',
    mix_voice_lufs: 'Voiceover loudness target (LUFS)',
    mix_ducking: 'Source ducking under voiceover',
    mix_attack_ms: 'Ducking attack (ms)',
    mix_release_ms: 'Ducking release (ms)',
    moss_max_chunk_seconds: 'Maximum MOSS context (seconds)',
    moss_chunk_overlap_seconds: 'MOSS chunk overlap (seconds)',
    moss_vad_enabled: 'Use VAD before MOSS diarization',
    moss_ctc_alignment_enabled: 'Align each MOSS turn to words with CTC',
    moss_ctc_aligner_model: 'MOSS CTC aligner model',
    moss_ctc_padding_seconds: 'MOSS turn CTC padding (seconds)',
    crispasr_vad_enabled: 'Use VAD for Whisper and Parakeet',
    crispasr_vad_model: 'VAD model',
    crispasr_vad_threshold: 'VAD speech threshold',
    crispasr_vad_min_speech_ms: 'Minimum detected speech (ms)',
    crispasr_vad_min_silence_ms: 'Silence required to end speech (ms)',
    crispasr_vad_max_speech_seconds: 'Maximum detected speech (seconds)',
    crispasr_vad_speech_pad_ms: 'Speech-edge padding (ms)',
    subtitle_hard_gap_ms: 'Hard subtitle boundary after silence (ms)',
    subtitle_sentence_boundary_threshold: 'Sentence boundary sensitivity',
    hard_gap_ms: 'Hard subtitle boundary after silence (ms)',
    sentence_boundary_threshold: 'Sentence boundary sensitivity',
    speech_block_continuation_threshold_ms:
      'Unfinished-sentence pause tolerance (ms)',
    speech_block_min_chars: 'Preferred minimum split size',
    speech_block_max_chars: 'Maximum TTS chunk size',
    speech_block_merge_threshold: 'Complete-utterance merge gap (ms)',
    speech_block_max_internal_gap_ms:
      'Maximum silence inside one TTS chunk (ms)'
  };
  if (labels[key]) return labels[key];
  return key
    .split('_')
    .map(
      (word) =>
        ACRONYMS[word.toLowerCase()] ??
        word.charAt(0).toUpperCase() + word.slice(1)
    )
    .join(' ')
    .replace(/ Ms\b/g, ' (ms)')
    .replace(/ Seconds\b/g, ' (seconds)');
}
