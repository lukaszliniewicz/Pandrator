export type PreviewableArtifact = {
  id: string;
  role?: string | null;
  kind?: string | null;
  relative_path?: string | null;
  path?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  state?: string | null;
  metadata_json?: Record<string, unknown> | null;
};

const ROLE_LABELS: Record<string, string> = {
  upload: 'Original source',
  source: 'Source file',
  transcription: 'Transcription',
  correction: 'Corrected subtitles',
  translation: 'Translated subtitles',
  reviewed_transcription: 'Reviewed transcription',
  reviewed_correction: 'Reviewed correction',
  reviewed_translation: 'Reviewed translation',
  subtitle_export: 'Subtitle export',
  tts_optimized: 'Speech-optimized text',
  tts_optimization: 'Speech-optimized text',
  clean_text: 'Cleaned source text',
  source_cleaned: 'Cleaned source text',
  speech_blocks: 'Speech generation blocks',
  generation_take: 'Generated audio take',
  dubbing_audio: 'Dubbing audio',
  audiobook_audio: 'Audiobook audio',
  rvc_audio: 'RVC-converted audio',
  rvc_model: 'RVC model',
  export: 'Final export',
  output_assembly: 'Assembled output',
  voice_sample: 'Voice sample',
  recording_upload: 'Recorded voice sample',
  training_manifest: 'Training manifest',
  waveform_peaks: 'Waveform preview',
  derived_pdf: 'Edited PDF',
  pdf_edit: 'Edited PDF'
};

export function humanizeIdentifier(value?: string | null) {
  if (!value) return 'Artifact';
  return value
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_./-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\w/, (letter) => letter.toUpperCase());
}

export function artifactRoleLabel(role?: string | null) {
  return ROLE_LABELS[String(role ?? '').toLowerCase()] ?? humanizeIdentifier(role);
}

export function artifactFilename(artifact: PreviewableArtifact) {
  const original = artifact.metadata_json?.original_filename;
  if (typeof original === 'string' && original.trim()) return original;
  const path = artifact.relative_path ?? artifact.path ?? '';
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? artifactRoleLabel(artifact.role);
}

export function formatBytes(value?: number | null) {
  if (value == null) return '';
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MB`;
  return `${(value / 1024 ** 3).toFixed(1)} GB`;
}
