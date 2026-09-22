import { dedupeInflight } from './inflight-dedupe';
import { sessionApi } from './domain-api';
import type { ItemPage, TtsCatalogue, VoiceRecord } from './api-models';

// Shared fetching for the TTS service catalogue and voice library.
//
// The session view mounts several components that each need the same data
// (SessionWorkspace, GenerationDrawer, GenerationCastPanel, voice tab).
// This module coalesces concurrent in-flight requests so one mount storm
// issues one fetch. It is deliberately inflight-only: settled slots are
// cleared and sequential callers always refetch, so voice and provider
// mutations stay immediately visible and no invalidation scheme is needed.
// Explicit refreshes (force=true) always start a fresh request.
const catalogueSlots = new Map<boolean, Promise<TtsCatalogue>>();
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

export function getVoiceLibrary(
  force = false
): Promise<ItemPage<VoiceRecord>> {
  return dedupeInflight(voicesSlots, 'voices', () => sessionApi.voices(), force);
}
