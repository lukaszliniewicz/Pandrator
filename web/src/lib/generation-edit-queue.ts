import type { GenerationSegmentPage } from './api-models';

/** Keep blur/save and click/regenerate ordered without requiring a manual pause. */
export class GenerationEditQueue {
  private pending = new Set<Promise<unknown>>();
  private replacements = new Map<string, string>();
  private failures = new Map<string, unknown>();
  private tail: Promise<unknown> = Promise.resolve();

  resolveId(id: string): string {
    const seen = new Set<string>();
    while (this.replacements.has(id) && !seen.has(id)) {
      seen.add(id);
      id = this.replacements.get(id)!;
    }
    return id;
  }

  recordReplacements(pairs: Iterable<readonly [string, string]>) {
    for (const [oldId, newId] of pairs) {
      if (oldId !== newId) this.replacements.set(oldId, newId);
    }
  }

  async save<T extends { id: string }>(
    oldId: string,
    operation: (currentId: string) => Promise<T>
  ): Promise<T> {
    // A content edit can clone the whole plan. Serialize saves so the next
    // blur uses the refreshed revision rather than racing its predecessor.
    const saving = this.tail
      .then(() => operation(this.resolveId(oldId)))
      .then(
        (updated) => {
          this.recordReplacements([
            [this.resolveId(oldId), updated.id],
            [oldId, updated.id]
          ]);
          for (const id of this.failures.keys()) {
            if (this.resolveId(id) === updated.id) this.failures.delete(id);
          }
          return updated;
        },
        (error: unknown) => {
          this.failures.set(oldId, error);
          throw error;
        }
      );
    this.tail = saving.catch(() => undefined);
    this.pending.add(saving);
    try {
      return await saving;
    } finally {
      this.pending.delete(saving);
    }
  }

  async settledIds(ids: string[]): Promise<string[]> {
    while (this.pending.size) await Promise.all([...this.pending]);
    // A save may already have failed before the Regenerate click. Never
    // silently generate the old text after displaying a save error.
    if (this.failures.size) throw this.failures.values().next().value;
    return [...new Set(ids.map((id) => this.resolveId(id)))];
  }
}

/** Enumerate stale rows in the whole pinned plan, not just a filtered viewport. */
export async function collectStaleSegmentIds(
  revisionId: string,
  fetchPage: (query: URLSearchParams) => Promise<GenerationSegmentPage>
): Promise<string[]> {
  const ids = new Set<string>();
  let cursor: number | null = null;
  const seen = new Set<number>();
  do {
    const query = new URLSearchParams({
      status: 'stale',
      plan_revision_id: revisionId,
      limit: '250'
    });
    if (cursor !== null) query.set('cursor', String(cursor));
    const page = await fetchPage(query);
    if (page.plan_revision_id !== revisionId) {
      throw new Error(
        'The speech plan changed. Refresh before regenerating stale segments.'
      );
    }
    for (const row of page.items) {
      if (row.status === 'stale' && !row.removed) ids.add(row.id);
    }
    cursor = page.next_cursor;
    if (cursor !== null) {
      if (seen.has(cursor))
        throw new Error('The stale-segment list could not be paged safely.');
      seen.add(cursor);
    }
  } while (cursor !== null);
  return [...ids];
}
