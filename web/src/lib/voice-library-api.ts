import { apiJson } from './api';

export type TraitEvidence = {
  source: 'user' | 'provider' | 'design_request' | 'audition_review';
  status: 'requested' | 'described' | 'reviewed';
  artifact_id?: string | null;
  note?: string | null;
};
export type VoiceLanguage = {
  language: string;
  locale?: string | null;
  accent?: string | null;
  detail?: string | null;
  evidence?: TraitEvidence | null;
};
export type VoiceProfile = {
  schema_version: 1;
  pitch: 'low' | 'mid' | 'high' | null;
  perceived_age: 'childlike' | 'youthful' | 'adult' | 'older' | null;
  textures: string[];
  delivery_presets: string[];
  use_cases: string[];
  languages: VoiceLanguage[];
  tags: string[];
  evidence: Record<string, TraitEvidence>;
};
export type VoiceReference =
  | { kind: 'managed'; voice_id: string }
  | { kind: 'provider'; service_id: string; model: string; voice: string };
export type VoiceCompatibility = {
  service_id: string;
  model: string;
  status: string;
  ready: boolean;
  voice?: string | null;
  supported_languages: string[];
  modes: {
    cloning: boolean;
    design: boolean;
    reference_with_instructions: boolean;
  };
};
export type CatalogVoice = {
  key: string;
  reference: VoiceReference;
  id: string;
  kind: 'managed' | 'provider';
  name: string;
  description: string;
  language?: string | null;
  voice_category: string;
  profile: VoiceProfile;
  origin: string;
  revision: number;
  collections: { id: string; name: string }[];
  compatibility: VoiceCompatibility[];
  preview_artifact_id?: string | null;
  bundled?: boolean;
  sample_count?: number;
  match_reasons?: string[];
};
export type VoiceCollection = {
  id: string;
  name: string;
  description?: string | null;
  revision: number;
  member_count: number;
  members: { key: string; reference: VoiceReference }[];
};
export type VoiceCatalogPage = {
  items: CatalogVoice[];
  total: number;
  next_cursor: string | null;
  facets: Record<string, Record<string, number>>;
  taxonomy: Record<string, string[]>;
  collections: Pick<
    VoiceCollection,
    'id' | 'name' | 'revision' | 'member_count'
  >[];
};
export const emptyVoiceProfile = (): VoiceProfile => ({
  schema_version: 1,
  pitch: null,
  perceived_age: null,
  textures: [],
  delivery_presets: [],
  use_cases: [],
  languages: [],
  tags: [],
  evidence: {}
});
export const voiceFacetLabel = (value: string) =>
  value.replaceAll('_', ' ').replace(/^\w/, (v) => v.toUpperCase());

export const voiceLibraryApi = {
  query: (params: Record<string, string | boolean | number | undefined>) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value));
    }
    return apiJson<VoiceCatalogPage>(`/voice-catalog?${query}`);
  },
  collections: () =>
    apiJson<{ items: VoiceCollection[] }>('/voice-collections'),
  createCollection: (name: string) =>
    apiJson<VoiceCollection>('/voice-collections', {
      method: 'POST',
      body: JSON.stringify({ name }),
      headers: { 'Content-Type': 'application/json' }
    }),
  membership: (
    collection: VoiceCollection,
    reference: VoiceReference,
    add: boolean
  ) =>
    apiJson<VoiceCollection>(
      `/voice-collections/${encodeURIComponent(collection.id)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: collection.revision,
          [add ? 'add_members' : 'remove_members']: [reference]
        })
      }
    ),
  update: (voice: CatalogVoice, changes: Record<string, unknown>) =>
    apiJson<CatalogVoice>(
      voice.kind === 'managed'
        ? `/voices/${encodeURIComponent(voice.id)}`
        : '/voice-catalog/metadata',
      {
        method: 'PATCH',
        headers: {
          'Content-Type': 'application/json',
          ...(voice.kind === 'managed'
            ? { 'If-Match': `"${voice.revision}"` }
            : {})
        },
        body: JSON.stringify(
          voice.kind === 'managed'
            ? changes
            : {
                reference: voice.reference,
                expected_revision: voice.revision,
                changes
              }
        )
      }
    )
};
