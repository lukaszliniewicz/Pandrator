// On-demand speech-plan revision detail: ticket-guarded, entry-scoped merges.
//
// Framework-free so delayed-response races are unit-testable without a
// browser (see web/unit-tests/speech-plan-history-detail.test.mjs).
// Lists load summaries (unchecked counts/eligibility); this loader fetches the
// exact audio-reuse counts and authoritative batch eligibility for one
// selected entry and merges them back without disturbing grouping decorations
// or sibling entries that happen to share a revision id.

export type DetailAudio = {
  segment_count: number;
  active_segment_count: number;
  reusable_segment_count: number | null;
  stale_segment_count: number | null;
  audio_settings_stale_segment_count?: number | null;
  audio_identity_unknown_segment_count?: number | null;
};

export type CheckedItem = {
  id: string;
  entry_id?: string;
  segment_count: number;
  active_segment_count: number;
  reusable_segment_count: number | null;
  stale_segment_count: number | null;
  audio_settings_stale_segment_count?: number | null;
  audio_identity_unknown_segment_count?: number | null;
  audio_reuse_checked?: boolean;
  repair_batch?: unknown;
};

export type DetailRequest = {
  entryKey: string;
  revisionId: string;
  needsAudio: boolean;
  needsBatch: boolean;
  batchId: string | null;
};

export function historyEntryKey(item: { id: string; entry_id?: string }): string {
  return item.entry_id ?? item.id;
}

export function mergeRevisionDetail<T extends CheckedItem>(
  items: T[],
  entryKey: string,
  revisionId: string,
  audio: DetailAudio | null,
  batch: T['repair_batch']
): T[] {
  return items.map((item) => {
    if (item.id !== revisionId) return item;
    let merged: T =
      audio === null
        ? { ...item }
        : {
            ...item,
            segment_count: audio.segment_count,
            active_segment_count: audio.active_segment_count,
            reusable_segment_count: audio.reusable_segment_count,
            stale_segment_count: audio.stale_segment_count,
            audio_settings_stale_segment_count:
              audio.audio_settings_stale_segment_count ?? null,
            audio_identity_unknown_segment_count:
              audio.audio_identity_unknown_segment_count ?? null,
            audio_reuse_checked: true
          };
    // Repair eligibility belongs to exactly one grouped entry: entries may
    // share a revision id across different repair batches, so only the
    // selected entry receives the authoritative batch.
    if (batch !== null && batch !== undefined && historyEntryKey(item) === entryKey) {
      merged = { ...merged, repair_batch: batch };
    }
    return merged;
  });
}

export type RevisionDetailSink<T> = {
  getItems: () => T[];
  setItems: (items: T[]) => void;
  setLoading: (loading: boolean) => void;
  setError: (message: string) => void;
  setActiveRevisionId: (id: string | null | undefined) => void;
};

export function createRevisionDetailLoader<T extends CheckedItem>(
  sink: RevisionDetailSink<T>,
  fetchers: {
    fetchRevision: (revisionId: string) => Promise<{
      revision: DetailAudio;
      active_revision_id: string | null | undefined;
    }>;
    fetchBatch: (batchId: string) => Promise<{ repair_batch: T['repair_batch'] }>;
  },
  toErrorMessage: (caught: unknown) => string
) {
  let ticket = 0;
  function invalidate() {
    ticket += 1;
    sink.setLoading(false);
    sink.setError('');
  }
  async function load(request: DetailRequest): Promise<void> {
    // Bump first: even a fully-cached selection invalidates a pending slow
    // row, so its late response can never overwrite this selection's state.
    const owned = ++ticket;
    if (!request.needsAudio && !request.needsBatch) {
      sink.setLoading(false);
      sink.setError('');
      return;
    }
    sink.setLoading(true);
    sink.setError('');
    try {
      const [audio, batch] = await Promise.all([
        request.needsAudio
          ? fetchers.fetchRevision(request.revisionId)
          : Promise.resolve(null),
        request.needsBatch && request.batchId
          ? fetchers.fetchBatch(request.batchId)
          : Promise.resolve(null)
      ]);
      if (owned !== ticket) return;
      sink.setItems(
        mergeRevisionDetail(
          sink.getItems(),
          request.entryKey,
          request.revisionId,
          audio === null ? null : audio.revision,
          batch === null ? null : batch.repair_batch
        )
      );
      if (audio && audio.active_revision_id !== undefined) {
        sink.setActiveRevisionId(audio.active_revision_id);
      }
    } catch (caught) {
      if (owned === ticket) sink.setError(toErrorMessage(caught));
    } finally {
      if (owned === ticket) sink.setLoading(false);
    }
  }
  return { load, invalidate };
}
