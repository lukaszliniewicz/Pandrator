type ReviewIdentity = { id?: string | null; draft_id?: string };

/** Resolve row copies to editable records, retaining first-match identity rules. */
export function createSubtitleReviewIndex<T extends ReviewIdentity>(
  records: readonly T[]
) {
  const objects = new Map<T, number>();
  const ids = new Map<string, number>();
  const drafts = new Map<string, number>();
  records.forEach((record, index) => {
    if (!objects.has(record)) objects.set(record, index);
    if (record.id && !ids.has(record.id)) ids.set(record.id, index);
    if (record.draft_id && !drafts.has(record.draft_id))
      drafts.set(record.draft_id, index);
  });
  function indexOf(record: T): number {
    let index = objects.get(record) ?? Infinity;
    if (record.id) index = Math.min(index, ids.get(record.id) ?? Infinity);
    if (record.draft_id)
      index = Math.min(index, drafts.get(record.draft_id) ?? Infinity);
    return index === Infinity ? -1 : index;
  }
  return {
    indexOf,
    canonical: (record: T): T => records[indexOf(record)] ?? record
  };
}
