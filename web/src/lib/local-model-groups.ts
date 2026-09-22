/**
 * Grouping logic for the local (audio.cpp) model chooser.
 *
 * The compact TTS catalogue path is retained: grouping consumes only slim
 * rows (`id`, `label`, `family`, `voice_mode`, `supported_languages`) plus
 * whatever richer fields a full/detail record carries. No per-model detail
 * fetching is required.
 *
 * Lifecycle honesty rules (load-bearing):
 * - `models[]` (selectable catalogue) is NEVER installed proof. It only
 *   guarantees an id appears as a choice.
 * - Badges come from explicit evidence only: live `loaded`,
 *   `status.installed`, or `package_availability` / `catalogue_info`
 *   package metadata. Absent evidence renders as Unknown.
 * - Absent, installed, and loaded stay three distinct states.
 * - Helpers are pure: they never drop ids, never rewrite ids, and never
 *   mutate their inputs.
 */

import { languageName, readable } from './audio-cpp-catalogue';

export type LocalModelCatalogEntry = {
  id: string;
  label?: unknown;
  display_name?: unknown;
  name?: unknown;
  family?: unknown;
  family_label?: unknown;
  voice_mode?: unknown;
  supported_languages?: unknown;
  languages?: unknown;
  precision?: unknown;
  recommended_for?: unknown;
  description?: unknown;
  loaded?: unknown;
  status?: unknown;
  package_availability?: unknown;
  catalogue_info?: unknown;
  [key: string]: unknown;
};

export type LocalModelBadge =
  | 'loaded'
  | 'installed'
  | 'installable'
  | 'gated'
  | 'unavailable'
  | 'catalogue_only'
  | 'unknown';

export type ResolvedLocalModel = {
  id: string;
  label: string;
  family: string;
  familyLabel: string;
  voiceMode: string;
  languages: string[];
  precision: string;
  precisionLabel: string;
  recommendedFor: string;
  description: string;
  badge: LocalModelBadge;
  badgeReason: string;
  /** In the service `models[]` selectable list. NOT installed proof. */
  listed: boolean;
  /** Not present in the catalogue rows at all (custom/retired id). */
  custom: boolean;
};

export type LocalModelSubgroup = {
  id: string;
  label: string;
  detail: string;
  voiceMode: string;
  models: ResolvedLocalModel[];
};

export type LocalModelGroup = {
  id: string;
  label: string;
  family: string;
  total: number;
  subgroups: LocalModelSubgroup[];
};

export const UNKNOWN_GROUP_ID = '__unknown';

const VOICE_MODE_LABELS: Record<string, string> = {
  cloning: 'Voice cloning',
  prebuilt: 'Built-in voices',
  hybrid: 'Built-in + cloning',
  optional_cloning: 'Optional cloning',
  design: 'Voice design',
  none: 'No voice use'
};

export function voiceModeLabel(mode: string): string {
  const normalized = mode.trim().toLowerCase();
  if (!normalized || normalized === 'unknown') return 'Unknown mode';
  return VOICE_MODE_LABELS[normalized] ?? readable(normalized);
}

export function badgeLabelFor(badge: LocalModelBadge): string {
  if (badge === 'loaded') return 'Loaded';
  if (badge === 'installed') return 'Installed';
  if (badge === 'unknown') return 'Unknown';
  return readable(badge);
}

/** Display-only family names. Metadata `family_label` always wins. */
const FAMILY_LABEL_OVERRIDES: Record<string, string> = {
  qwen3_tts: 'Qwen3-TTS',
  qwen3_asr: 'Qwen3-ASR',
  pocket_tts: 'PocketTTS',
  voxcpm1: 'VoxCPM1',
  voxcpm2: 'VoxCPM2',
  magpie_tts: 'MagpieTTS',
  fireredtts3: 'FireRedTTS3',
  breeze_tts: 'BreezeTTS',
  cosyvoice3: 'CosyVoice3',
  dots_tts: 'DotTTS',
  f5_tts: 'F5-TTS',
  glm_tts: 'GLM-TTS',
  index_tts2: 'IndexTTS2',
  neutts: 'NeuTTS',
  omnivoice: 'OmniVoice',
  outetts: 'OuteTTS',
  miotts: 'MioTTS',
  mira_tts: 'MiraTTS',
  fish_audio: 'Fish Audio S2',
  fish_audio_s2: 'Fish Audio S2',
  chatterbox_turbo: 'Chatterbox Turbo',
  kokoro_tts: 'Kokoro',
  higgs_audio_tts: 'Higgs Audio TTS',
  vibevoice: 'VibeVoice',
  moonshine_asr: 'Moonshine ASR',
  parakeet_tdt: 'Parakeet-TDT',
  sortformer_diar: 'Sortformer Diarization',
  sortformer_diar_v2: 'Sortformer Diarization v2.1'
};

const FAMILY_TOKEN_LABELS: Record<string, string> = {
  tts: 'TTS',
  tts2: 'TTS2',
  asr: 'ASR',
  stt: 'STT',
  vc: 'VC',
  vad: 'VAD',
  midi: 'MIDI',
  s2: 'S2',
  s2s: 'S2S',
  ai: 'AI'
};

function prettifyFamily(family: string): string {
  const label = family
    .split(/[_-]+/)
    .filter(Boolean)
    .map(
      (token) =>
        FAMILY_TOKEN_LABELS[token.toLowerCase()] ??
        token.charAt(0).toUpperCase() + token.slice(1)
    )
    .join(' ');
  return label || family;
}

export function familyDisplayName(
  family: string,
  metadataLabel = '',
  overrides: Record<string, string> = {}
): string {
  if (metadataLabel.trim()) return metadataLabel.trim();
  return (
    overrides[family] ??
    FAMILY_LABEL_OVERRIDES[family] ??
    prettifyFamily(family)
  );
}

/** Controlled precision vocabulary. Metadata `precision` always wins. */
const PRECISION_TOKENS: Array<{ pattern: RegExp; label: string }> = [
  { pattern: /(^|[_-])q8dit($|[_-])/, label: 'Q8 DiT' },
  { pattern: /(^|[_-])q8_0($|[_-])/, label: 'Q8_0' },
  { pattern: /(^|[_-])q4_0($|[_-])/, label: 'Q4_0' },
  { pattern: /(^|[_-])q4_1($|[_-])/, label: 'Q4_1' },
  { pattern: /(^|[_-])q5_0($|[_-])/, label: 'Q5_0' },
  { pattern: /(^|[_-])q5_1($|[_-])/, label: 'Q5_1' },
  { pattern: /(^|[_-])bf16($|[_-])/, label: 'BF16' },
  { pattern: /(^|[_-])f16($|[_-])/, label: 'F16' },
  { pattern: /(^|[_-])f32($|[_-])/, label: 'F32' },
  { pattern: /(^|[_-])safetensors($|[_-])/, label: 'Safetensors' },
  { pattern: /(^|[_-])orig(inal)?($|[_-])/, label: 'Original' }
];

const PRECISION_LABELS: Record<string, string> = {
  q8_0: 'Q8_0',
  q8dit: 'Q8 DiT',
  q4_0: 'Q4_0',
  q4_1: 'Q4_1',
  q5_0: 'Q5_0',
  q5_1: 'Q5_1',
  bf16: 'BF16',
  f16: 'F16',
  f32: 'F32',
  safetensors: 'Safetensors',
  orig: 'Original',
  original: 'Original'
};

function precisionFromId(modelId: string): string {
  const haystack = modelId.toLowerCase();
  for (const token of PRECISION_TOKENS) {
    if (token.pattern.test(haystack)) return token.label;
  }
  return '';
}

function precisionLabelFor(raw: string): string {
  const normalized = raw.trim().toLowerCase();
  if (!normalized) return '';
  return PRECISION_LABELS[normalized] ?? raw.trim();
}

const SIZE_TOKENS: Array<{ pattern: RegExp; label: string; rank: number }> = [
  { pattern: /(^|[_-])1_7b($|[_-])/, label: '1.7B', rank: 0 },
  { pattern: /(^|[_-])0_6b($|[_-])/, label: '0.6B', rank: 1 }
];

const QWEN_VARIANT_ORDER = ['Base', 'CustomVoice', 'VoiceDesign', 'Other'];

function qwenVariant(voiceMode: string, modelId: string): string {
  const mode = voiceMode.trim().toLowerCase();
  if (mode === 'cloning') return 'Base';
  if (mode === 'prebuilt') return 'CustomVoice';
  if (mode === 'design') return 'VoiceDesign';
  const haystack = `_${modelId.toLowerCase()}_`;
  if (haystack.includes('_voicedesign_')) return 'VoiceDesign';
  if (haystack.includes('_customvoice_')) return 'CustomVoice';
  if (haystack.includes('_base_')) return 'Base';
  return 'Other';
}

const POCKET_LANGUAGE_TOKENS: Record<string, string> = {
  english: 'en',
  german: 'de',
  italian: 'it',
  portuguese: 'pt',
  spanish: 'es',
  french: 'fr'
};

function pocketLanguageCode(languages: string[], modelId: string): string {
  if (languages.length) return languages[0].toLowerCase();
  const haystack = `_${modelId.toLowerCase()}_`;
  for (const [token, code] of Object.entries(POCKET_LANGUAGE_TOKENS)) {
    if (haystack.includes(`_${token}_`)) return code;
  }
  return '';
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const codes: string[] = [];
  for (const item of value) {
    if (typeof item === 'string' && item.trim()) codes.push(item.trim());
    else if (item && typeof item === 'object') {
      const record = item as Record<string, unknown>;
      const code = asString(record.code ?? record.id ?? record.language);
      if (code.trim()) codes.push(code.trim());
    }
  }
  return [...new Set(codes)];
}

function installedFlag(value: unknown): boolean | null {
  const record = asRecord(value);
  if (typeof record.installed === 'boolean') return record.installed;
  return null;
}

function availabilityOf(value: unknown): { status: string; reason: string } {
  if (typeof value === 'string')
    return { status: value.trim().toLowerCase(), reason: '' };
  const record = asRecord(value);
  return {
    status: asString(record.status).trim().toLowerCase(),
    reason: asString(record.reason)
  };
}

function availabilityBadge(status: string): LocalModelBadge {
  if (status === 'installable') return 'installable';
  if (status === 'gated') return 'gated';
  if (status === 'unavailable') return 'unavailable';
  if (status === 'catalogued_only' || status === 'catalogued-only')
    return 'catalogue_only';
  return 'unknown';
}

export type ResolveOptions = {
  voiceModes?: Record<string, string>;
  loadedIds?: Iterable<string>;
  /**
   * Genuine installed evidence (for example manager inspection
   * `installed_model_ids`). Unlike `listedIds` this DOES drive the
   * Installed badge. Never pass the selectable catalogue here.
   */
  installedIds?: Iterable<string>;
  familyLabels?: Record<string, string>;
};

/**
 * Merge catalogue rows, the selectable `models[]` id list, and the current
 * selection into one ordered, annotated leaf list. Catalogue order is
 * preserved; ids missing from rows are appended as custom leaves so the
 * current selection is never lost.
 */
export function resolveLocalModels(
  catalog: LocalModelCatalogEntry[],
  listedIds: Iterable<string> = [],
  extraIds: Iterable<string> = [],
  options: ResolveOptions = {}
): ResolvedLocalModel[] {
  const listed = new Set(
    [...listedIds].map((id) => String(id)).filter(Boolean)
  );
  const loaded = new Set(
    [...(options.loadedIds ?? [])].map((id) => String(id)).filter(Boolean)
  );
  const installed = new Set(
    [...(options.installedIds ?? [])].map((id) => String(id)).filter(Boolean)
  );
  const voiceModes = options.voiceModes ?? {};
  const byId = new Map<string, LocalModelCatalogEntry>();
  for (const entry of catalog ?? []) {
    const id = String(entry?.id ?? '').trim();
    if (id && !byId.has(id)) byId.set(id, entry);
  }
  const orderedIds = [...byId.keys()];
  for (const id of [...listed, ...extraIds].map(String)) {
    if (id && !byId.has(id)) orderedIds.push(id);
  }
  return orderedIds.map((id) => {
    const entry = byId.get(id);
    const info = asRecord(entry?.catalogue_info);
    const label =
      asString(entry?.label) ||
      asString(entry?.display_name) ||
      asString(entry?.name) ||
      asString(info.label) ||
      id;
    const family = asString(entry?.family) || asString(info.family) || '';
    const familyLabel = familyDisplayName(
      family,
      asString(entry?.family_label) || asString(info.family_label),
      options.familyLabels ?? {}
    );
    const voiceMode =
      asString(entry?.voice_mode) ||
      asString(voiceModes[id]) ||
      asString(info.voice_mode) ||
      '';
    const languages = [
      ...new Set([
        ...asStringList(entry?.supported_languages),
        ...asStringList(entry?.languages),
        ...asStringList(info.supported_languages)
      ])
    ];
    const precisionRaw = asString(entry?.precision) || asString(info.precision);
    const precision = precisionRaw || precisionFromId(id);
    const loadedSignal =
      entry?.loaded === true || info.loaded === true || loaded.has(id);
    const installedSignal =
      installedFlag(entry?.status) ??
      installedFlag(info.status) ??
      installed.has(id);
    const availability = availabilityOf(
      entry?.package_availability ?? info.package_availability
    );
    const badge: LocalModelBadge = loadedSignal
      ? 'loaded'
      : installedSignal
        ? 'installed'
        : availabilityBadge(availability.status);
    return {
      id,
      label,
      family,
      familyLabel,
      voiceMode: voiceMode.trim().toLowerCase(),
      languages,
      precision,
      precisionLabel: precisionLabelFor(precision),
      recommendedFor:
        asString(entry?.recommended_for) || asString(info.recommended_for),
      description: asString(entry?.description) || asString(info.description),
      badge,
      badgeReason: availability.reason,
      listed: listed.has(id),
      custom: !entry
    };
  });
}

function subgroupFor(
  family: string,
  members: ResolvedLocalModel[]
): LocalModelSubgroup[] {
  if (family === 'qwen3_tts') {
    // Manager rows often carry no voice_mode; the variant identity implies
    // the capability (Base clones, CustomVoice uses built-ins, VoiceDesign
    // designs), matching the backend catalogue mapping.
    const impliedMode: Record<string, string> = {
      Base: 'cloning',
      CustomVoice: 'prebuilt',
      VoiceDesign: 'design',
      Other: ''
    };
    const buckets = new Map<string, LocalModelSubgroup>();
    for (const model of members) {
      const size =
        SIZE_TOKENS.find((token) =>
          token.pattern.test(model.id.toLowerCase())
        ) ?? null;
      const variant = qwenVariant(model.voiceMode, model.id);
      const key = `${size?.label ?? 'Other size'}:${variant}`;
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = {
          id: `qwen:${key}`,
          label: `${size?.label ?? 'Other size'} ${variant}`,
          detail: voiceModeLabel(model.voiceMode || impliedMode[variant] || ''),
          voiceMode: model.voiceMode,
          models: []
        };
        buckets.set(key, bucket);
      }
      bucket.models.push(model);
    }
    return [...buckets.values()].sort((left, right) => {
      const leftSize = SIZE_TOKENS.find((token) =>
        left.id.includes(token.label)
      )?.rank;
      const rightSize = SIZE_TOKENS.find((token) =>
        right.id.includes(token.label)
      )?.rank;
      const sizeRank = (leftSize ?? 99) - (rightSize ?? 99);
      if (sizeRank !== 0) return sizeRank;
      return (
        QWEN_VARIANT_ORDER.indexOf(left.label.split(' ').slice(-1)[0]) -
        QWEN_VARIANT_ORDER.indexOf(right.label.split(' ').slice(-1)[0])
      );
    });
  }
  if (family === 'pocket_tts') {
    const buckets = new Map<string, LocalModelSubgroup>();
    for (const model of members) {
      const code = pocketLanguageCode(model.languages, model.id);
      const key = code || 'other';
      let bucket = buckets.get(key);
      if (!bucket) {
        bucket = {
          id: `language:${key}`,
          label: code ? languageName(code) : 'Other language',
          detail: voiceModeLabel(model.voiceMode),
          voiceMode: model.voiceMode,
          models: []
        };
        buckets.set(key, bucket);
      }
      bucket.models.push(model);
    }
    return [...buckets.values()].sort((left, right) =>
      left.label.localeCompare(right.label)
    );
  }
  const modes = [...new Set(members.map((model) => model.voiceMode))];
  if (modes.length > 1) {
    return modes
      .map((mode) => ({
        id: `mode:${mode || 'unknown'}`,
        label: voiceModeLabel(mode),
        detail: '',
        voiceMode: mode,
        models: members.filter((model) => model.voiceMode === mode)
      }))
      .sort((left, right) => left.label.localeCompare(right.label));
  }
  const mode = modes[0] ?? '';
  return [
    {
      id: 'all',
      label: '',
      detail: mode ? voiceModeLabel(mode) : '',
      voiceMode: mode,
      models: [...members]
    }
  ];
}

/** Nest resolved leaves as family -> variant/language -> precision. */
export function groupLocalModels(
  models: ResolvedLocalModel[]
): LocalModelGroup[] {
  const buckets = new Map<string, ResolvedLocalModel[]>();
  for (const model of models ?? []) {
    const key = model.family || UNKNOWN_GROUP_ID;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(model);
    else buckets.set(key, [model]);
  }
  const groups: LocalModelGroup[] = [];
  for (const [family, members] of buckets) {
    const known = family !== UNKNOWN_GROUP_ID;
    groups.push({
      id: known ? `family:${family}` : UNKNOWN_GROUP_ID,
      label: known ? members[0]?.familyLabel || family : 'Other models',
      family: known ? family : '',
      total: members.length,
      subgroups: known
        ? subgroupFor(family, members)
        : [
            {
              id: 'all',
              label: '',
              detail: '',
              voiceMode: '',
              models: [...members]
            }
          ]
    });
  }
  return groups.sort((left, right) => {
    if (left.id === UNKNOWN_GROUP_ID) return 1;
    if (right.id === UNKNOWN_GROUP_ID) return -1;
    return left.label.localeCompare(right.label);
  });
}

function haystackFor(
  group: LocalModelGroup,
  subgroup: LocalModelSubgroup,
  model: ResolvedLocalModel
): string {
  return [
    model.id,
    model.label,
    group.label,
    subgroup.label,
    subgroup.detail,
    voiceModeLabel(model.voiceMode),
    model.precisionLabel,
    model.precision,
    badgeLabelFor(model.badge),
    ...model.languages,
    ...model.languages.map((code) => languageName(code)),
    model.recommendedFor
  ]
    .join(' ')
    .toLowerCase();
}

/**
 * Prune groups to leaves matching the query, so search reaches collapsed
 * descendants. Returns fresh group objects; inputs are untouched.
 */
export function filterLocalGroups(
  groups: LocalModelGroup[],
  query: string
): LocalModelGroup[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return groups;
  const pruned: LocalModelGroup[] = [];
  for (const group of groups ?? []) {
    const subgroups: LocalModelSubgroup[] = [];
    for (const subgroup of group.subgroups) {
      const models = subgroup.models.filter((model) =>
        haystackFor(group, subgroup, model).includes(needle)
      );
      if (models.length) subgroups.push({ ...subgroup, models });
    }
    if (subgroups.length)
      pruned.push({
        ...group,
        total: subgroups.reduce(
          (count, subgroup) => count + subgroup.models.length,
          0
        ),
        subgroups
      });
  }
  return pruned;
}

export function findLocalModel(
  groups: LocalModelGroup[],
  id: string
): {
  group: LocalModelGroup;
  subgroup: LocalModelSubgroup;
  model: ResolvedLocalModel;
} | null {
  if (!id) return null;
  for (const group of groups ?? []) {
    for (const subgroup of group.subgroups) {
      const model = subgroup.models.find((leaf) => leaf.id === id);
      if (model) return { group, subgroup, model };
    }
  }
  return null;
}

/** Readable trigger summary. The id itself is never rewritten. */
export function summarizeLocalModel(
  groups: LocalModelGroup[],
  id: string,
  emptyLabel = 'Choose a model'
): string {
  if (!id) return emptyLabel;
  const found = findLocalModel(groups, id);
  if (!found) return id;
  const { group, subgroup, model } = found;
  if (group.id === UNKNOWN_GROUP_ID) return model.label;
  const parts = [group.label];
  if (subgroup.label) parts.push(subgroup.label);
  parts.push(model.precisionLabel || model.label);
  return parts.join(' · ');
}
