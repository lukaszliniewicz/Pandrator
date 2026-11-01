import { dedupeInflight } from './inflight-dedupe';
import { sessionApi } from './domain-api';
import { browserStorageKey } from './browser-identity';
import type { SpeechCatalogueScope } from './speech-catalogue-api';
import type {
  ItemPage,
  TtsCompactCatalogue,
  TtsServiceDetailResponse,
  VoiceRecord
} from './api-models';

// Shared fetching for the TTS service catalogue and voice library.
//
// The session view mounts several components that each need the same data
// (SessionWorkspace, GenerationDrawer, GenerationCastPanel, voice tab).
// This module coalesces concurrent in-flight requests so one mount storm
// issues one fetch. It is deliberately inflight-only: settled slots are
// cleared and sequential callers always refetch, so voice and provider
// mutations stay immediately visible and no invalidation scheme is needed.
// Explicit refreshes (force=true) always start a fresh request.
//
// Pass 2 adds the compact catalogue (slim rows for dropdowns: models,
// voices, voice_catalogues, slim model_catalog, voice_metadata, defaults,
// capability flags) and per-service/model detail hydration. Consumers keep
// a monotonic request id (epoch) per selection and apply a response only
// when it is still the latest, so a late full-catalogue or older detail
// response never clobbers a newer selection.
const compactSlots = new Map<string, Promise<TtsCompactCatalogue>>();
const detailSlots = new Map<string, Promise<TtsServiceDetailResponse>>();
const voicesSlots = new Map<string, Promise<ItemPage<VoiceRecord>>>();

export function speechCatalogueKey(scope: SpeechCatalogueScope = {}) {
  return browserStorageKey(
    'speech-catalogue',
    scope.sessionId,
    scope.workspaceId
  );
}

export function getTtsCompactCatalogue(
  refresh = false,
  force = false,
  scope: SpeechCatalogueScope = {}
): Promise<TtsCompactCatalogue> {
  return dedupeInflight(
    compactSlots,
    `${speechCatalogueKey(scope)}:${refresh}`,
    () => sessionApi.ttsCatalogueCompact(refresh, scope),
    force
  );
}

export type TtsDetailSelection = SpeechCatalogueScope & {
  refresh?: boolean;
  models?: string[] | string;
  force?: boolean;
};

function normalizeDetailKey(
  serviceId: string,
  selection: TtsDetailSelection
): string {
  const models =
    selection.models == null
      ? []
      : (Array.isArray(selection.models)
          ? selection.models
          : [selection.models]
        )
          .map((model) => model.trim())
          .filter(Boolean)
          .sort();
  return [
    speechCatalogueKey(selection),
    serviceId.trim().toLowerCase(),
    models.join(','),
    selection.refresh ? 'refresh' : 'cached'
  ].join(':');
}

export function getTtsServiceDetail(
  serviceId: string,
  selection: TtsDetailSelection = {}
): Promise<TtsServiceDetailResponse> {
  const key = normalizeDetailKey(serviceId, selection);
  return dedupeInflight(
    detailSlots,
    key,
    () =>
      sessionApi.ttsServiceDetail(serviceId, {
        refresh: selection.refresh,
        models: selection.models,
        workspaceId: selection.workspaceId,
        sessionId: selection.sessionId
      }),
    selection.force ?? false
  );
}

export function getVoiceLibrary(
  force = false,
  scope: SpeechCatalogueScope = {}
): Promise<ItemPage<VoiceRecord>> {
  return dedupeInflight(
    voicesSlots,
    speechCatalogueKey(scope),
    () => sessionApi.voices(scope),
    force
  );
}
