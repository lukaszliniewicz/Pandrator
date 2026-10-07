import { translationProjectApi } from './domain-api';
import type { TranslationProjectPayload } from './api-models';
import { ResourceState } from './resource-state.svelte';
import { invalidationBus } from './invalidation';

export class TranslationProjectStore {
  private readonly resource =
    new ResourceState<TranslationProjectPayload | null>(null);
  private refreshing = false;
  constructor(readonly sessionId: string) {}
  get value() {
    return this.resource.value;
  }
  get loading() {
    return this.resource.loading;
  }
  get error() {
    return this.resource.error;
  }
  load(force = false) {
    return this.resource.load(
      () => translationProjectApi.forSession(this.sessionId, 'compact'),
      { force }
    );
  }
  markStale() {
    this.resource.markStale();
  }
  connect() {
    return invalidationBus.subscribe((batch) => {
      const related = new Set([
        this.sessionId,
        ...(this.value?.project?.branches.map((branch) => branch.session_id) ??
          [])
      ]);
      if (
        !batch.session_ids.some((id) => related.has(id)) ||
        !batch.resources.some((resource) =>
          ['sessions', 'workflow', 'sources'].includes(resource)
        )
      )
        return;
      this.markStale();
      void this.refresh();
    });
  }
  private async refresh() {
    if (this.refreshing) return;
    this.refreshing = true;
    try {
      do {
        await this.load().catch(() => undefined);
      } while (this.resource.status === 'stale');
    } finally {
      this.refreshing = false;
    }
  }
}
