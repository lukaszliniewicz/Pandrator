import { sessionApi } from './domain-api';
import type { LoadState, OutcomePlan, SessionRecord } from './api-models';
import {
  invalidates,
  invalidationBus,
  type InvalidationBatch
} from './invalidation';
import { ResourceState } from './resource-state.svelte';

type SessionBundle = {
  session: SessionRecord;
  outcome: OutcomePlan;
};

export class SessionStore {
  private readonly resource = new ResourceState<SessionBundle | null>(null);
  private unsubscribe?: () => void;
  private reloadQueued = false;

  constructor(
    public sessionId: string,
    private readonly onSession: (session: SessionRecord) => void
  ) {}

  get session() {
    return this.resource.value?.session ?? null;
  }

  get outcome() {
    return this.resource.value?.outcome ?? null;
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
    const bundle = await this.resource.load(
      async () => {
        const [session, outcome] = await Promise.all([
          sessionApi.get(sessionId),
          sessionApi.outcome(sessionId)
        ]);
        return { session, outcome };
      },
      {
        force,
        isCurrent: () => this.sessionId === sessionId
      }
    );
    // A retarget() during flight resets the resource to null, so a stale
    // bundle can never satisfy this guard with a mismatched id.
    if (bundle && bundle.session.id === this.sessionId)
      this.onSession(bundle.session);
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
    // Show the loading state (not "Session not found") until reload lands.
    this.resource.status = 'loading';
    return true;
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
    if (
      invalidates(batch, 'sessions', this.sessionId) ||
      invalidates(batch, 'workflow', this.sessionId)
    ) {
      this.resource.markStale();
      void this.reloadCoalesced();
    }
  }

  /**
   * Reload, draining mid-flight invalidations. If a batch arrives while a
   * request is in flight, markStale() records it and the resolver leaves
   * status 'stale'; the loop then issues exactly one trailing reload and
   * converges once no new invalidations arrive. Concurrent callers coalesce
   * on reloadQueued instead of storming the backend.
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
}
