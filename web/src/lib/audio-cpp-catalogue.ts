export type AudioCppModelInfo = {
  id: string;
  label: string;
  family: string;
  family_label?: string;
  category?: string;
  description?: string;
  upstream_status?: string;
  supported_languages?: string[];
  language_note?: string;
  unlisted_languages?: boolean;
  recommended_for?: string;
  reference_audio?: string;
  reference_text?: string;
  estimated_download_bytes?: number;
  license?: { name?: string; url?: string; commercial_use?: string };
  upstream_features?: Record<string, boolean>;
  pandrator_features?: Record<string, string>;
  package_availability?: string | { status?: string; reason?: string };
  verified_runtime?: string;
  sources?: string[];
};

export type AudioCppCatalogue = {
  runtime_version: string;
  total: number;
  offset: number;
  next_offset: number | null;
  items: AudioCppModelInfo[];
  families: { id: string; display_name: string; category: string }[];
};

export const featureLabels: Record<string, string> = {
  voice_cloning: 'Voice cloning',
  voice_design: 'Voice design',
  instructions: 'Natural-language directions',
  emotion_control: 'Emotion controls',
  vocal_events: 'Vocal sounds',
  multi_speaker: 'Multiple speakers in one request',
  streaming: 'Streaming',
  speech_editing: 'Speech editing',
  voice_conversion: 'Voice conversion',
  sound_generation: 'Sound effects',
  music_generation: 'Music generation',
  audio_insertion: 'Insert a recording',
  native_multi_speaker: 'Multiple speakers in one request',
  speech_generation: 'Speech generation',
  acoustic_verification: 'Listening validation',
  timing: 'Exact timing'
};

export function readable(value?: string): string {
  const labels: Record<string, string> = {
    not_used: 'Not used',
    required_for_cloning: 'Required when cloning',
    required_for_linked_reference: 'Required for linked recordings',
    not_implemented: 'Not implemented',
    not_tested: 'Not yet tested',
    request_supported: 'Request supported',
    catalogued_only: 'Catalogue only',
    noncommercial: 'Non-commercial',
    permitted_with_attribution: 'Attribution required',
    conditional: 'Subject to licence restrictions',
    unknown: 'Not verified',
    unverified: 'Not verified',
    field: 'Direction field',
    inline: 'Inline controls',
    none: 'Not supported',
    documented: 'Documented'
  };
  if (!value) return 'Not verified';
  return (
    labels[value] ??
    value.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase())
  );
}

export function languageName(code: string): string {
  try {
    return new Intl.DisplayNames(['en'], { type: 'language' }).of(code) ?? code;
  } catch {
    return code;
  }
}
