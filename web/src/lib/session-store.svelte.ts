import { sessionApi } from './domain-api';
import type {
  LoadState,
  OutcomePlan,
  SessionRecord
} from './api-models';
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

  constructor(
    readonly sessionId: string,
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
    const bundle = await this.resource.load(
      async () => {
        const [session, outcome] = await Promise.all([
          sessionApi.get(this.sessionId),
          sessionApi.outcome(this.sessionId)
        ]);
        return { session, outcome };
      },
      { force }
    );
    if (bundle) this.onSession(bundle.session);
  }

  connect() {
    if (this.unsubscribe) return this.unsubscribe;
    this.unsubscribe = invalidationBus.subscribe((batch) => this.invalidate(batch));
    return () => {
      this.unsubscribe?.();
      this.unsubscribe = undefined;
    };
  }

  private invalidate(batch: InvalidationBatch) {
    if (
      invalidates(batch, 'sessions', this.sessionId)
      || invalidates(batch, 'workflow', this.sessionId)
    ) {
      this.resource.markStale();
      this.load().catch(() => undefined);
    }
  }
}
