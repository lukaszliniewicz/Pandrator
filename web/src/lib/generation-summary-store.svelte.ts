import { generationApi } from './domain-api';
import type { GenerationSummary } from './api-models';
import { ResourceState } from './resource-state.svelte';
import { invalidates, invalidationBus } from './invalidation';

/** The collapsed drawer needs counts and activity, not rows, takes or history. */
export class GenerationSummaryStore {
  private readonly resource = new ResourceState<GenerationSummary | null>(null);
  private controller?: AbortController;
  private timer?: number;
  private connected = false;
  constructor(readonly sessionId: string) {}
  get value() {
    return this.resource.value;
  }
  get status() {
    return this.resource.status;
  }
  get error() {
    return this.resource.error;
  }

  async load(force = false) {
    return this.resource.load(
      () => {
        this.controller?.abort();
        this.controller = new AbortController();
        return generationApi.summary(this.sessionId, this.controller.signal);
      },
      { force }
    );
  }

  connect() {
    this.connected = true;
    void this.load().catch(() => undefined);
    const disconnect = invalidationBus.subscribe((batch) => {
      const progressForCurrentRun = batch.events.some(
        (event) =>
          event.type === 'job.progress' &&
          event.session_id === this.sessionId &&
          ((event.generation_run_id &&
            event.generation_run_id === this.value?.active_run?.id) ||
            (event.job_id && event.job_id === this.value?.active_run?.job_id))
      );
      if (
        !invalidates(batch, 'generation', this.sessionId) &&
        !invalidates(batch, 'output', this.sessionId) &&
        !progressForCurrentRun
      )
        return;
      this.resource.markStale();
      // Throttle progress bursts; refresh compact usage/counts without rows or history.
      if (
        batch.events.length &&
        batch.events.every((event) => event.type === 'job.progress')
      ) {
        if (this.timer === undefined)
          this.timer = window.setTimeout(() => {
            this.timer = undefined;
            void this.refresh();
          }, 1_000);
        return;
      }
      if (this.timer !== undefined) window.clearTimeout(this.timer);
      this.timer = window.setTimeout(() => {
        this.timer = undefined;
        void this.refresh();
      }, 600);
    });
    return () => {
      this.connected = false;
      disconnect();
      this.controller?.abort();
      if (this.timer !== undefined) window.clearTimeout(this.timer);
      this.timer = undefined;
    };
  }

  private async refresh() {
    if (!this.connected) return;
    await this.load().catch(() => undefined);
    if (this.connected && this.status === 'stale' && this.timer === undefined)
      this.timer = window.setTimeout(() => {
        this.timer = undefined;
        void this.refresh();
      }, 600);
  }
}
