import { dedupeInflight } from './inflight-dedupe';
import { sessionApi } from './domain-api';
import type {
  ItemPage,
  TtsCatalogue,
  TtsCompactCatalogue,
  TtsService,
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
const catalogueSlots = new Map<boolean, Promise<TtsCatalogue>>();
const compactSlots = new Map<boolean, Promise<TtsCompactCatalogue>>();
const detailSlots = new Map<string, Promise<TtsServiceDetailResponse>>();
const voicesSlots = new Map<'voices', Promise<ItemPage<VoiceRecord>>>();

export function getTtsCatalogue(
  refresh = false,
  force = false
): Promise<TtsCatalogue> {
  return dedupeInflight(
    catalogueSlots,
    refresh,
    () => sessionApi.ttsCatalogue(refresh),
    force
  );
}

export function getTtsCompactCatalogue(
  refresh = false,
  force = false
): Promise<TtsCompactCatalogue> {
  return dedupeInflight(
    compactSlots,
    refresh,
    () => sessionApi.ttsCatalogueCompact(refresh),
    force
  );
}

export type TtsDetailSelection = {
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
        models: selection.models
      }),
    selection.force ?? false
  );
}

const DETAIL_MODEL_MAP_KEYS = [
  'voice_catalogues',
  'voice_metadata',
  'model_voice_modes',
  'default_voices',
  'generation_prompt_models'
] as const;

/** Merge a hydrated full/detail service over slim compact rows.
 *
 * Per-model maps merge by key with the detail entry winning, and
 * `model_catalog` merges by model id, so a model-filtered detail (one
 * full record) layered over compact rows keeps the full chooser breadth:
 * the chosen model carries its full record while every other model keeps
 * its slim summary. Inputs are not mutated.
 */
export function mergeServiceDetail(
  services: TtsService[],
  detail: TtsService
): TtsService[] {
  const match = (item: TtsService) =>
    [item.id, item.name].some(
      (value) =>
        String(value ?? '').toLowerCase() ===
        String(detail.id ?? '').toLowerCase()
    );
  let replaced = false;
  const merged = services.map((item) => {
    if (!match(item)) return item;
    replaced = true;
    const next: TtsService = { ...item, ...detail };
    for (const key of DETAIL_MODEL_MAP_KEYS) {
      const base = item[key];
      const over = detail[key];
      if (
        base &&
        over &&
        typeof base === 'object' &&
        typeof over === 'object' &&
        !Array.isArray(base) &&
        !Array.isArray(over)
      ) {
        (next as Record<string, unknown>)[key] = { ...base, ...over };
      } else if (Array.isArray(base) && Array.isArray(over)) {
        (next as Record<string, unknown>)[key] = Array.from(
          new Set([...base.map(String), ...over.map(String)])
        );
      }
    }
    const baseCatalog = Array.isArray(item.model_catalog)
      ? item.model_catalog
      : [];
    const overCatalog = Array.isArray(detail.model_catalog)
      ? detail.model_catalog
      : [];
    if (baseCatalog.length || overCatalog.length) {
      const byId = new Map(
        baseCatalog.map((entry) => [String(entry?.id ?? ''), entry])
      );
      for (const entry of overCatalog) byId.set(String(entry?.id ?? ''), entry);
      next.model_catalog = [...byId.values()];
    }
    return next;
  });
  return replaced ? merged : [...services, detail];
}

export function getVoiceLibrary(force = false): Promise<ItemPage<VoiceRecord>> {
  return dedupeInflight(
    voicesSlots,
    'voices',
    () => sessionApi.voices(),
    force
  );
}
