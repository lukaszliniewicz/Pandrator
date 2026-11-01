import { apiJson } from './api';
import { dedupeInflight } from './inflight-dedupe';
import type { TtsCatalogueObservation } from './api-models';
import { browserStorageKey } from './browser-identity';
import {
  speechScopeQuery,
  type SpeechCatalogueScope
} from './speech-catalogue-api';

type VoiceWorkspaceInfo = {
  workspace_id?: string;
  can_edit?: boolean;
};

type TraitEvidence = {
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
  catalogue?: TtsCatalogueObservation | null;
  service_id: string;
  model: string;
  status: string;
  ready: boolean;
  prepared?: boolean;
  service_available?: boolean | null;
  health_checked_at?: string | null;
  availability_reason?: string | null;
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
export type VoiceCollection = VoiceWorkspaceInfo & {
  id: string;
  name: string;
  description?: string | null;
  revision: number;
  member_count: number;
  members: { key: string; reference: VoiceReference }[];
};
export type VoiceCatalogPage = VoiceWorkspaceInfo & {
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
export const voiceOriginLabel = (value: string) =>
  (
    ({
      designed: 'Designed',
      imported: 'Cloned / reference',
      builtin: 'Built-in',
      built_in: 'Built-in',
      provider: 'Provider voice',
      unknown: 'Unspecified'
    }) as Record<string, string>
  )[value] ?? voiceFacetLabel(value);

const catalogRequests = new Map<string, Promise<VoiceCatalogPage>>();

function workspacePath(path: string, workspaceId?: string) {
  const query = speechScopeQuery({ workspaceId });
  return query.size ? `${path}?${query}` : path;
}

export const voiceLibraryApi = {
  managedVoice: async (
    id: string,
    scope: SpeechCatalogueScope = {}
  ): Promise<CatalogVoice | null> => {
    // Cast fields mount together. Share one inventory request, then fall back
    // to an exact lookup for libraries larger than the first page.
    const page = await voiceLibraryApi.query(
      { kind: 'managed', limit: 100 },
      scope
    );
    const voice = page.items.find((item) => item.id === id);
    if (voice || !page.next_cursor) return voice ?? null;
    const exact = await voiceLibraryApi.query(
      {
        query: id,
        kind: 'managed',
        limit: 1
      },
      scope
    );
    return exact.items.find((item) => item.id === id) ?? null;
  },
  query: (
    params: Record<string, string | boolean | number | undefined>,
    scope: SpeechCatalogueScope = {}
  ) => {
    const query = speechScopeQuery(scope);
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== '') query.set(key, String(value));
    }
    query.sort();
    const path = `/voice-catalog?${query}`;
    return dedupeInflight(
      catalogRequests,
      browserStorageKey('voice-catalog', path),
      () => apiJson<VoiceCatalogPage>(path)
    );
  },
  collections: (scope: SpeechCatalogueScope = {}) => {
    const query = speechScopeQuery(scope);
    return apiJson<VoiceWorkspaceInfo & { items: VoiceCollection[] }>(
      query.size ? `/voice-collections?${query}` : '/voice-collections'
    );
  },
  createCollection: (name: string, workspaceId?: string) =>
    apiJson<VoiceCollection>(workspacePath('/voice-collections', workspaceId), {
      method: 'POST',
      body: JSON.stringify({ name }),
      headers: { 'Content-Type': 'application/json' }
    }),
  membership: (
    collection: VoiceCollection,
    reference: VoiceReference,
    add: boolean,
    workspaceId?: string
  ) =>
    apiJson<VoiceCollection>(
      workspacePath(
        `/voice-collections/${encodeURIComponent(collection.id)}`,
        workspaceId
      ),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: collection.revision,
          [add ? 'add_members' : 'remove_members']: [reference]
        })
      }
    ),
  updateCollection: (
    collection: VoiceCollection,
    changes: {
      name?: string;
      add_members?: VoiceReference[];
      remove_members?: VoiceReference[];
    },
    workspaceId?: string
  ) =>
    apiJson<VoiceCollection>(
      workspacePath(
        `/voice-collections/${encodeURIComponent(collection.id)}`,
        workspaceId
      ),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          expected_revision: collection.revision,
          ...changes
        })
      }
    ),
  update: (
    voice: CatalogVoice,
    changes: Record<string, unknown>,
    workspaceId?: string
  ) =>
    apiJson<CatalogVoice>(
      workspacePath(
        voice.kind === 'managed'
          ? `/voices/${encodeURIComponent(voice.id)}`
          : '/voice-catalog/metadata',
        workspaceId
      ),
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
