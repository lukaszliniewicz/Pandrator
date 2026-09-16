import type { GenerationSegment } from './api-models';

/**
 * Pure segment-edit adoption helpers (no Svelte reactivity, no I/O).
 *
 * A TTS-text save on a used plan clones the plan (edit-copy) with new
 * segment IDs. The mutation response is authoritative: it carries the new
 * segment ID, its new plan_revision_id, and previous_segment_id. The store
 * must adopt all three synchronously so regenerate correctness never
 * depends on a follow-up reload winning a race with event-driven refreshes.
 */

export type SegmentPageState = {
  items: GenerationSegment[];
  plan_revision_id: string | null;
};

function staleMarkedTakes(
  previousTakes: GenerationSegment['takes'],
  updated: GenerationSegment
): GenerationSegment['takes'] {
  if (updated.takes?.length) return updated.takes;
  return (previousTakes ?? []).map((take) =>
    updated.status === 'stale' && take.status === 'completed'
      ? { ...take, status: 'stale' }
      : take
  );
}

/** Adopt one single-segment mutation response. Throws on inconsistency. */
export function adoptUpdatedSegment(
  page: SegmentPageState,
  itemId: string,
  updated: GenerationSegment
): SegmentPageState {
  if (updated.id !== itemId) {
    // Edit copy: the new segment ID must arrive with its new plan revision.
    // Without the pin the response cannot be adopted safely.
    if (!updated.plan_revision_id) {
      throw new Error(
        'The segment edit returned a new block without its new speech-plan revision. Reload before regenerating.'
      );
    }
    // A new segment ID must never inherit the old row's take IDs: take
    // identity is per segment. Takes stay cleared until the reload delivers
    // the authoritative cloned-take data.
    const adopted: GenerationSegment = { ...updated, takes: [] };
    const present = page.items.some((candidate) => candidate.id === itemId);
    return {
      items: present
        ? page.items.map((candidate) =>
            candidate.id === itemId ? adopted : candidate
          )
        : [...page.items, adopted],
      plan_revision_id: updated.plan_revision_id
    };
  }
  return {
    items: page.items.map((candidate) =>
      candidate.id === itemId
        ? {
            ...candidate,
            ...updated,
            takes: staleMarkedTakes(candidate.takes, updated)
          }
        : candidate
    ),
    plan_revision_id: updated.plan_revision_id ?? page.plan_revision_id
  };
}

/** Adopt one batch mutation response. Throws on inconsistent mixed plans. */
export function adoptUpdatedSegments(
  page: SegmentPageState,
  resultItems: GenerationSegment[]
): SegmentPageState {
  const copies = resultItems.filter(
    (item) => item.previous_segment_id && item.previous_segment_id !== item.id
  );
  const pins = new Set(
    resultItems
      .map((item) => item.plan_revision_id)
      .filter((value): value is string => Boolean(value))
  );
  if (copies.length > 0 && pins.size !== 1) {
    // Copied rows without exactly one owning revision cannot be adopted:
    // the pin would be ambiguous and regenerating could target the wrong
    // revision's text.
    throw new Error(
      'The segment edits returned an inconsistent set of speech-plan revisions. Reload before regenerating.'
    );
  }
  const byPrevious = new Map(
    copies.map((item) => [item.previous_segment_id as string, item])
  );
  const byId = new Map(resultItems.map((item) => [item.id, item]));
  const consumedPrevious = new Set(byPrevious.keys());
  const items: GenerationSegment[] = page.items.flatMap((candidate) => {
    const copied = byPrevious.get(candidate.id);
    if (copied) return [{ ...copied, takes: [] }];
    const updated = byId.get(candidate.id);
    if (!updated) return [candidate];
    return [
      {
        ...candidate,
        ...updated,
        takes: staleMarkedTakes(candidate.takes, updated)
      }
    ];
  });
  const seen = new Set(items.map((candidate) => candidate.id));
  for (const item of resultItems) {
    // Skip rows already adopted above via previous-ID remapping or same-ID
    // merge, and skip stale old IDs consumed by a copy. Anything else with a
    // genuinely new ID (e.g. a copy whose previous row was never loaded in
    // this paged viewport) still appears under its new ID, without old takes.
    if (seen.has(item.id) || consumedPrevious.has(item.id)) continue;
    const copied = copies.includes(item);
    items.push(copied ? { ...item, takes: [] } : item);
    seen.add(item.id);
  }
  return {
    items,
    plan_revision_id:
      copies.length > 0 ? ([...pins][0] as string) : page.plan_revision_id
  };
}

/**
 * A load started at loadEpoch must not apply once the store has adopted a
 * newer mutation (currentEpoch). The store also aborts the in-flight request;
 * this guard covers an already-resolved response whose continuation runs
 * after the adoption.
 */
export function isStaleLoad(loadEpoch: number, currentEpoch: number): boolean {
  return loadEpoch !== currentEpoch;
}
