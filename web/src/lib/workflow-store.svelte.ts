import { artifactRoleLabel } from './artifact-display';
import { sessionApi } from './domain-api';
import type { LoadState, WorkflowSnapshot, WorkflowStage } from './api-models';
import {
  invalidates,
  invalidationBus,
  type InvalidationBatch
} from './invalidation';
import { ResourceState } from './resource-state.svelte';

function presentSnapshot(snapshot: WorkflowSnapshot) {
  return {
    ...snapshot,
    stages: snapshot.stages.map((stage) => ({
      ...stage,
      artifact: stage.artifact
        ? {
            ...stage.artifact,
            raw_role: stage.artifact.role,
            role: artifactRoleLabel(stage.artifact.role)
          }
        : null
    }))
  };
}

export class WorkflowStore {
  private readonly resource = new ResourceState<WorkflowSnapshot | null>(null);
  private unsubscribe?: () => void;
  private reloadQueued = false;

  constructor(public sessionId: string) {}

  get snapshot() {
    return this.resource.value;
  }

  get status(): LoadState {
    return this.resource.status;
  }

  get loading() {
    return this.resource.loading;
  }

  get error() {
    return this.resource.error;
  }

  async load(force = false) {
    const sessionId = this.sessionId;
    return this.resource.load(
      async () => presentSnapshot(await sessionApi.workflow(sessionId)),
      {
        force,
        isCurrent: () => this.sessionId === sessionId
      }
    );
  }

  /**
   * Point this store at a different session (SvelteKit reuses the layout
   * across [id] navigations). reset() bumps the resource epoch, so an
   * in-flight load for the old id is discarded instead of overwriting the
   * new session's state. Returns true when the id actually changed.
   */
  retarget(sessionId: string) {
    if (sessionId === this.sessionId) return false;
    this.sessionId = sessionId;
    this.resource.reset(null);
    // Show the loading state until reload lands.
    this.resource.status = 'loading';
    return true;
  }

  refresh() {
    return this.load(true);
  }

  replace(snapshot: WorkflowSnapshot) {
    this.resource.replace(snapshot);
  }

  connect() {
    if (this.unsubscribe) return this.unsubscribe;
    this.unsubscribe = invalidationBus.subscribe((batch) =>
      this.invalidate(batch)
    );
    return () => {
      this.unsubscribe?.();
      this.unsubscribe = undefined;
    };
  }

  private invalidate(batch: InvalidationBatch) {
    this.patchLiveProgress(batch);
    if (invalidates(batch, 'workflow', this.sessionId)) {
      this.resource.markStale();
      void this.reloadCoalesced();
    }
  }

  /**
   * Reload, draining mid-flight invalidations (see SessionStore). Concurrent
   * callers coalesce on reloadQueued instead of storming the backend.
   */
  private async reloadCoalesced() {
    if (this.reloadQueued) return;
    this.reloadQueued = true;
    try {
      for (;;) {
        await this.load().catch(() => undefined);
        if (this.resource.status !== 'stale') break;
      }
    } finally {
      this.reloadQueued = false;
    }
  }

  private patchLiveProgress(batch: InvalidationBatch) {
    const snapshot = this.snapshot;
    if (!snapshot) return;
    let changed = false;
    const stages = snapshot.stages.map((stage): WorkflowStage => {
      const update = batch.events.find(
        (event) =>
          event.session_id === this.sessionId &&
          event.job_id &&
          event.job_id === stage.job_id
      );
      if (!update) return stage;
      changed = true;
      return {
        ...stage,
        ...(update.progress !== undefined && stage.progress_basis !== 'segments'
          ? { progress: Number(update.progress) }
          : {}),
        ...(update.detail !== undefined ? { detail: update.detail } : {}),
        ...(['queued', 'running', 'cancel_requested'].includes(
          String(update.status ?? '')
        )
          ? { status: 'running' as const }
          : {})
      };
    });
    if (changed) this.replace({ ...snapshot, stages });
  }
}
