import type { PreviewableArtifact } from './artifact-display';
import { LANGUAGE_OPTIONS } from './settings-fields';

export type StageArtifact = PreviewableArtifact & {
  id: string;
  role: string;
  version: number;
  created_at: string;
  is_selected: boolean;
  parent_ids: string[];
  settings_hash?: string | null;
};

export type ArtifactDetail = {
  label: string;
  value: string;
};

const dateFormatter = new Intl.DateTimeFormat(undefined, {
  dateStyle: 'medium',
  timeStyle: 'short'
});

const languageLabels = new Map(
  LANGUAGE_OPTIONS.map((option) => [String(option.value).toLowerCase(), option.label])
);

const engineLabels: Record<string, string> = {
  whisper: 'Whisper large-v3',
  parakeet: 'Parakeet TDT 0.6B v3'
};

const backendLabels: Record<string, string> = {
  llm: 'LLM provider',
  deepl: 'DeepL',
  cpu: 'CPU',
  cuda: 'CUDA',
  vulkan: 'Vulkan',
  metal: 'Apple Metal',
  auto: 'Automatic'
};

function metadataText(metadata: Record<string, unknown>, ...keys: string[]) {
  for (const key of keys) {
    const value = metadata[key];
    if (value != null && String(value).trim()) return String(value).trim();
  }
  return '';
}

function titleCase(value: string) {
  return value
    .replaceAll('_', ' ')
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function formatArtifactDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : dateFormatter.format(date);
}

export function artifactOptionLabel(artifact: StageArtifact) {
  const details = artifact.metadata_json ?? {};
  const rawDescriptor = metadataText(details, 'model', 'model_name', 'engine', 'backend');
  const descriptor = engineLabels[rawDescriptor.toLowerCase()] ?? backendLabels[rawDescriptor.toLowerCase()] ?? rawDescriptor;
  return `v${artifact.version} · ${descriptor ? `${descriptor} · ` : ''}${formatArtifactDate(artifact.created_at)}`;
}

export function artifactDetails(artifact: StageArtifact): ArtifactDetail[] {
  const metadata = artifact.metadata_json ?? {};
  const engine = metadataText(metadata, 'engine', 'stt_engine').toLowerCase();
  const quantization = metadataText(metadata, 'model_quantization', 'quantization').toUpperCase();
  const rawModel = metadataText(
    metadata,
    'model',
    'model_name',
    'correction_model',
    'translation_model',
    'tts_optimization_model',
    'llm_model'
  );
  const model = rawModel || engineLabels[engine] || engine;
  const language = metadataText(metadata, 'language', 'target_language', 'source_language');
  const backend = metadataText(metadata, 'backend', 'translation_backend').toLowerCase();
  const compute = metadataText(metadata, 'compute_backend', 'stt_compute_backend').toLowerCase();
  const revision = metadataText(metadata, 'revision');

  const details: ArtifactDetail[] = [];
  if (model) {
    const displayModel = engineLabels[model.toLowerCase()] ?? model;
    details.push({ label: 'Model', value: quantization ? `${displayModel} · ${quantization}` : displayModel });
  }
  if (backend) details.push({ label: 'Backend', value: backendLabels[backend] ?? titleCase(backend) });
  if (language) details.push({ label: 'Language', value: languageLabels.get(language.toLowerCase()) ?? language });
  if (compute) details.push({ label: 'Compute', value: backendLabels[compute] ?? titleCase(compute) });
  if (metadata.reviewed === true) details.push({ label: 'Edit', value: revision ? `Reviewed revision ${revision}` : 'Manually reviewed' });
  return details;
}
