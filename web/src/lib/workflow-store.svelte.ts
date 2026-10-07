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

function historyIdentity(stage: WorkflowStage) {
  return JSON.stringify([
    stage.selection_revision ?? 0,
    stage.selected_artifact_id ?? null,
    stage.artifact_history_total ?? 0,
    stage.artifacts?.[0]?.id ?? null
  ]);
}

function retainHistory(
  previous: WorkflowSnapshot | null,
  next: WorkflowSnapshot
) {
  if (previous?.session_id !== next.session_id) return next;
  return {
    ...next,
    stages: next.stages.map((stage) => {
      const prior = previous.stages.find((item) => item.key === stage.key);
      if (
        !prior ||
        historyIdentity(prior) !== historyIdentity(stage) ||
        (prior.artifacts?.length ?? 0) <= (stage.artifacts?.length ?? 0)
      )
        return stage;
      const merged = new Map(
        [...(prior.artifacts ?? []), ...(stage.artifacts ?? [])].map((item) => [
          item.id,
          item
        ])
      );
      return {
        ...stage,
        artifacts: [...merged.values()].sort((a, b) => b.version - a.version),
        artifact_history_has_more: prior.artifact_history_has_more,
        artifact_history_next_before_version:
          prior.artifact_history_next_before_version
      };
    })
  };
}

export class WorkflowStore {
  private readonly resource = new ResourceState<WorkflowSnapshot | null>(null);
  private unsubscribe?: () => void;
  private reloadQueued = false;
  private historyEpoch = 0;
  historyLoading = $state<Record<string, boolean>>({});

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

  markStale() {
    this.resource.markStale();
  }

  async load(force = false) {
    const sessionId = this.sessionId;
    return this.resource.load(
      async () =>
        retainHistory(
          this.snapshot,
          presentSnapshot(await sessionApi.workflow(sessionId))
        ),
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
    this.historyEpoch++;
    this.historyLoading = {};
    this.resource.reset(null);
    // Show the loading state until reload lands.
    this.resource.status = 'loading';
    return true;
  }

  refresh() {
    return this.load(true);
  }

  replace(snapshot: WorkflowSnapshot) {
    this.resource.replace(retainHistory(this.snapshot, snapshot));
  }

  async loadStageHistory(stageKey: string) {
    const stage = this.snapshot?.stages.find((item) => item.key === stageKey);
    const beforeVersion = stage?.artifact_history_next_before_version;
    if (!stage || beforeVersion == null || this.historyLoading[stageKey])
      return;
    const sessionId = this.sessionId;
    const epoch = this.historyEpoch;
    const identity = historyIdentity(stage);
    const currentStage = () => {
      if (epoch !== this.historyEpoch || sessionId !== this.sessionId) return;
      const current = this.snapshot?.stages.find(
        (item) => item.key === stageKey
      );
      return current &&
        historyIdentity(current) === identity &&
        current.artifact_history_next_before_version === beforeVersion
        ? current
        : undefined;
    };
    this.historyLoading[stageKey] = true;
    try {
      const page = await sessionApi.stageArtifacts(
        sessionId,
        stageKey,
        beforeVersion
      );
      const current = currentStage();
      if (
        !current ||
        page.revision !== (current.selection_revision ?? 0) ||
        page.selected_artifact_id !== (current.selected_artifact_id ?? null) ||
        page.total !== (current.artifact_history_total ?? 0)
      )
        return;
      const merged = new Map(
        [...(current.artifacts ?? []), ...page.items].map((item) => [
          item.id,
          item
        ])
      );
      this.resource.replace({
        ...this.snapshot!,
        stages: this.snapshot!.stages.map((item) =>
          item.key !== stageKey
            ? item
            : {
                ...current,
                artifacts: [...merged.values()].sort(
                  (a, b) => b.version - a.version
                ),
                artifact_history_total: page.total,
                artifact_history_has_more: page.has_more,
                artifact_history_next_before_version: page.next_before_version
              }
        )
      });
    } catch (caught) {
      if (currentStage()) throw caught;
    } finally {
      if (epoch === this.historyEpoch && sessionId === this.sessionId)
        this.historyLoading[stageKey] = false;
    }
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
